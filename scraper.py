"""
Async scraping of Binance announcements + leaderboards.

Binance pages are JS-rendered, so we use one shared headless Chromium (Playwright).
For leaderboards we intercept the JSON XHR responses the page makes, which is far more
robust than parsing DOM. A semaphore limits concurrency; a delay is applied between requests.
"""
import asyncio
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl
from playwright.async_api import async_playwright, Browser, TimeoutError as PWTimeout
from config import REQUEST_DELAY_SEC
from reward_caps import extract_reward_cap

log = logging.getLogger("scraper")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MONTHS = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?"
          r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")
DATE_RE = re.compile(rf"(\d{{4}}[-/.]\d{{1,2}}[-/.]\d{{1,2}}(?:[ T]+\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?)"
                     rf"|((?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}(?:,?\s+(?:at\s+)?\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?)", re.I)
PAIR_RE = re.compile(r"\b([A-Z0-9]{2,15})/(USDT|USDC|FDUSD|BTC|BNB|ETH|TRY|EUR)\b")
POOL_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(BNB|USDT|USDC|FDUSD)\b[^.\n]{0,40}(?:pool|reward|prize)", re.I)
MINVOL_RE = re.compile(r"(?:minimum|at least)[^$\n]{0,60}\$?\s*([\d,]+(?:\.\d+)?)\s*(?:USD|USDT)?", re.I)
LINK_RE = re.compile(r"https?://(?:www\.)?binance\.com/[^\s)\]]*(?:support/announcement|activity|campaign)[^\s)\]]*")


def _parse_date(s: str) -> int | None:
    """Parse a Binance date (always UTC). Returns None instead of raising."""
    if not s:
        return None
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s.strip(), flags=re.I)
    s = re.sub(r"\bat\b", " ", s, flags=re.I).replace(",", " ")
    if re.match(r"\d{4}", s):
        s = re.sub(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})", r"\1-\2-\3", s).replace("T", " ")
    else:
        s = re.sub(r"^([A-Za-z]+)\.", r"\1", s)
    s = " ".join(s.split())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%B %d %Y %H:%M:%S", "%B %d %Y %H:%M", "%B %d %Y",
                "%b %d %Y %H:%M:%S", "%b %d %Y %H:%M", "%b %d %Y"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    if s[:4].lower() == "sept":
        return _parse_date("Sep" + s[4:])
    return None


def _to_float(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def parse_period(text: str) -> tuple[int | None, int | None]:
    m = re.search(r"(?:Promotion|Activity|Campaign|Event|Tournament)\s+Period[:\s]*(.{0,200})", text or "", re.I | re.S)
    if not m:
        return None, None
    stamps = [t for t in (_parse_date(d[0] or d[1]) for d in DATE_RE.findall(m.group(1))) if t]
    start = stamps[0] if stamps else None
    end = stamps[1] if len(stamps) > 1 else None
    if start and end and end < start:
        start, end = end, start
    return start, end


def harvest_rows(obj, rows: dict, depth: int = 0):
    """Collect {rank: volume} from any nested JSON. Never raises on odd shapes."""
    if depth > 12:
        return
    if isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict):
                rk = next((it[k] for k in ("rank", "ranking", "rankNo") if it.get(k) is not None), None)
                vol = next((it[k] for k in ("volume", "tradeVolume", "tradingVolume", "value", "amount")
                            if it.get(k) is not None), None)
                r, v = _to_float(rk), _to_float(vol)
                if r is not None and v is not None and r >= 1 and r == int(r):
                    rows[int(r)] = v
                    continue
            harvest_rows(it, rows, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            harvest_rows(v, rows, depth + 1)


def invalid_ranked_rows(obj, depth=0):
    if depth > 12:
        return False
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and any(k in item for k in ("rank", "ranking", "rankNo")):
                rank = next((item[k] for k in ("rank", "ranking", "rankNo") if item.get(k) is not None), None)
                vol = next((item[k] for k in ("volume", "tradeVolume", "tradingVolume", "value", "amount") if item.get(k) is not None), None)
                r, v = _to_float(rank), _to_float(vol)
                if r is None or r < 1 or r != int(r) or v is None:
                    return True
            elif invalid_ranked_rows(item, depth + 1):
                return True
    elif isinstance(obj, dict):
        return any(invalid_ranked_rows(v, depth + 1) for v in obj.values())
    return False


def response_identity(resp):
    parsed = urlsplit(resp.url)
    paging = {"page", "p", "currentpage", "pagenum", "pagenumber", "offset", "start"}
    params = tuple(sorted((k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in paging))
    post = resp.request.post_data
    if post:
        import json
        try:
            body = json.loads(post)
            if isinstance(body, dict):
                post = json.dumps({k: v for k, v in body.items() if k.lower() not in paging}, sort_keys=True)
        except (ValueError, TypeError):
            pass
    return parsed.scheme, parsed.netloc, parsed.path, params, resp.request.method, post


class IncompleteLeaderboard(RuntimeError):
    """Never publish a partial leaderboard as a successful snapshot."""


@dataclass
class LeaderboardResult:
    volumes: list[float]
    ranked_participants: int
    total_participants: int | None = None
    pages: int = 1


def _count(value):
    number = _to_float(value)
    return int(number) if number is not None and number == int(number) else None


def participant_total(obj, depth=0):
    """Only explicit joined/participant fields; generic `total` may mean ranked rows."""
    if depth > 12:
        return None
    if isinstance(obj, dict):
        for key in ("totalParticipants", "participantCount", "joinedCount", "totalJoined",
                    "totalParticipantCount"):
            if key in obj and (count := _count(obj[key])) is not None:
                return count
        for value in obj.values():
            count = participant_total(value, depth + 1)
            if count is not None:
                return count
    elif isinstance(obj, list):
        for value in obj:
            count = participant_total(value, depth + 1)
            if count is not None:
                return count
    return None


def pagination_metadata(obj, depth=0):
    """Read pagination only from an envelope containing actual ranked rows."""
    if depth > 12 or not isinstance(obj, dict):
        return {}
    local = {}
    harvest_rows(obj, local)
    if local:
        meta = {}
        for name, aliases in {
            "page": ("currentPage", "pageNum", "pageNumber", "page"),
            "pages": ("totalPages", "pageCount"),
            "ranked_total": ("total", "totalCount", "totalRecords"),
        }.items():
            for key in aliases:
                if (value := _count(obj.get(key))) is not None:
                    meta[name] = value
                    break
        for value in obj.values():
            if isinstance(value, dict):
                meta.update(pagination_metadata(value, depth + 1))
        return meta
    # Empty pages can still advance when the API supplies an explicit page number.
    for value in obj.values():
        meta = pagination_metadata(value, depth + 1)
        if meta:
            return meta
    if any(key in obj for key in ("list", "rows", "records")):
        return {name: value for name, key in (("page", "currentPage"), ("pages", "totalPages"),
                                             ("ranked_total", "total"))
                if (value := _count(obj.get(key))) is not None}
    return {}


def is_binance_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and host in {"binance.com", "www.binance.com"}
                and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        return False


def extract_links(text: str) -> list[str]:
    return [u for u in dict.fromkeys(LINK_RE.findall(text or "")) if is_binance_url(u)]


class Scraper:
    def __init__(self, concurrency: int = 2):
        self._pw = None
        self.browser: Browser | None = None
        self.sem = asyncio.Semaphore(concurrency)
        self._launch_lock = asyncio.Lock()

    async def start(self):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])

    async def stop(self):
        for closer in (self.browser and self.browser.close, self._pw and self._pw.stop):
            if closer:
                try:
                    await closer()
                except Exception:
                    log.debug("browser shutdown error", exc_info=True)
        self.browser = self._pw = None

    async def _ensure(self):
        async with self._launch_lock:  # concurrent callers must not launch two browsers
            if not self.browser or not self.browser.is_connected():
                log.warning("Browser not connected, relaunching")
                await self.stop()
                await self.start()

    @staticmethod
    async def _close_ctx(ctx):
        try:
            await ctx.close()
        except Exception:
            log.debug("context close failed", exc_info=True)

    async def _page_text(self, url: str) -> tuple[str, list[str]]:
        await self._ensure()
        async with self.sem:
            ctx = await self.browser.new_context(user_agent=UA, locale="en-US")
            try:
                page = await ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass  # Binance keeps polling; DOM content is enough
                await page.wait_for_timeout(1500)
                text = await page.inner_text("body", timeout=15000)
                try:
                    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                except Exception:
                    hrefs = []
                return text or "", [h for h in hrefs if isinstance(h, str)]
            finally:
                await self._close_ctx(ctx)
                await asyncio.sleep(REQUEST_DELAY_SEC)

    async def scrape_announcement(self, url: str) -> dict:
        if not is_binance_url(url):
            raise ValueError("Send a valid HTTPS Binance announcement link.")
        text, hrefs = await self._page_text(url)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        title = next((l for l in lines if len(l) > 15 and any(
            k in l.lower() for k in ("tournament", "trade", "share", "campaign", "promotion", "earn"))), lines[0] if lines else url)

        if not lines:
            raise ValueError("Announcement page was empty or blocked.")
        start_ts, end_ts = parse_period(text)

        pairs = sorted({f"{a}/{b}" for a, b in PAIR_RE.findall(text)})
        pools = [(v, t.upper()) for a, t in POOL_RE.findall(text) if (v := _to_float(a))]
        total_pool, token = max(pools) if pools else (None, "BNB")
        mv = MINVOL_RE.search(text)
        min_volume = _to_float(mv.group(1)) if mv else None

        rules_idx = next((i for i, l in enumerate(lines) if "rule" in l.lower() or "eligib" in l.lower()), None)
        rules = "\n".join(lines[rules_idx:rules_idx + 6]) if rules_idx is not None else ""

        reward_cap, reward_cap_token = extract_reward_cap(text, token)
        landing = next((h for h in hrefs if "/activity/" in h or "/campaign/" in h or "leaderboard" in h.lower()), None)
        return {
            "url": url, "title": title[:200], "start_ts": start_ts, "end_ts": end_ts,
            "pairs": pairs, "rules": rules[:1500], "min_volume": min_volume,
            "total_pool": total_pool, "remaining_pool": None, "pool_token": token,
            "fixed_slots": [], "leaderboard_url": landing,
            "reward_cap": reward_cap, "reward_cap_token": reward_cap_token,
        }

    async def scrape_leaderboard(self, url: str, skip_top: int) -> LeaderboardResult:
        """Walk every page sequentially; no page-count or whole-run time limit.

        Each page must acknowledge advancement before another click. A failed,
        repeated, blocked or unparseable page raises rather than returning partial data.
        Per-request timeouts prevent hung requests; jobs retain the last complete snapshot.
        """
        await self._ensure()
        rows: dict[int, float] = {}
        metadata = {}
        total_joined = None
        revision = 0
        last_signature = None
        tasks = set()
        source_identity = None
        expected_page = 1
        capture_error = None

        async def capture(resp):
            nonlocal total_joined, revision, last_signature, source_identity, capture_error
            try:
                if not resp.ok or "json" not in (resp.headers.get("content-type") or ""):
                    return
                identity = response_identity(resp)
                if source_identity is not None and identity != source_identity:
                    return
                payload = await resp.json()
                if source_identity is not None and identity != source_identity:
                    return
                found = {}
                harvest_rows(payload, found)
                meta = pagination_metadata(payload)
                if not found and not meta:
                    return
                if source_identity is None:
                    source_identity = identity
                if invalid_ranked_rows(payload):
                    capture_error = IncompleteLeaderboard("Malformed ranked participant; snapshot not saved")
                    revision += 1
                    return
                if meta.get("page") is not None and meta["page"] != expected_page:
                    capture_error = IncompleteLeaderboard("Unexpected leaderboard page; snapshot not saved")
                    revision += 1
                    return
                joined = participant_total(payload)
                if joined is not None:
                    total_joined = joined
                if not found and not meta:
                    return
                signature = (meta.get("page"), hash(tuple(sorted(found.items()))))
                if signature != last_signature:
                    rows.update(found)
                    metadata.update(meta)
                    last_signature = signature
                    revision += 1
            except Exception:
                log.debug("Unreadable leaderboard response", exc_info=True)

        def on_response(resp):
            task = asyncio.create_task(capture(resp))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def wait_advance(previous):
            # Wait on parsed responses, not a fixed sleep or row-count growth.
            async def wait():
                while revision <= previous:
                    await asyncio.sleep(0.05)
            try:
                await asyncio.wait_for(wait(), timeout=30)
                if capture_error is not None:
                    raise capture_error
            except asyncio.TimeoutError as exc:
                raise IncompleteLeaderboard("Leaderboard page did not advance; snapshot not saved") from exc

        async with self.sem:
            ctx = await self.browser.new_context(user_agent=UA, locale="en-US")
            page = None
            try:
                page = await ctx.new_page()
                page.on("response", on_response)
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await wait_advance(0)
                pages = 1
                seen = {last_signature}
                while True:
                    # Let loading/disabled states settle AFTER response parsing.
                    await asyncio.sleep(max(REQUEST_DELAY_SEC, 0.1))
                    nxt = page.locator("button[aria-label*='next' i], .bn-pagination-next, li[title='Next Page']").first
                    exists = await nxt.count() > 0
                    disabled = exists and (not await nxt.is_enabled() or
                        await nxt.get_attribute("aria-disabled") == "true" or
                        "disabled" in ((await nxt.get_attribute("class")) or ""))
                    if disabled and (metadata.get("pages", pages) > pages or
                                     metadata.get("ranked_total", len(rows)) > len(rows)):
                        async def wait_enabled():
                            while not await nxt.is_enabled() or await nxt.get_attribute("aria-disabled") == "true" or "disabled" in ((await nxt.get_attribute("class")) or ""):
                                await asyncio.sleep(0.05)
                        try:
                            await asyncio.wait_for(wait_enabled(), timeout=30)
                        except asyncio.TimeoutError as exc:
                            raise IncompleteLeaderboard("Next stayed disabled before the final page") from exc
                        continue
                    if not exists or disabled:
                        if not any(k in metadata for k in ("pages", "ranked_total")):
                            raise IncompleteLeaderboard("Disabled/missing Next without verified final-page metadata")
                        if metadata.get("pages", pages) > pages:
                            raise IncompleteLeaderboard("Next control ended before the reported final page")
                        expected = metadata.get("ranked_total")
                        if expected is not None and len(rows) != expected:
                            raise IncompleteLeaderboard(f"Captured {len(rows)} ranked users; source reports {expected}")
                        break
                    if not await nxt.is_visible():
                        raise IncompleteLeaderboard("Next-page control is hidden before completion")
                    previous = revision
                    old_page = metadata.get("page")
                    expected_page = pages + 1
                    await nxt.click(timeout=10000)
                    await wait_advance(previous)
                    if last_signature in seen:
                        raise IncompleteLeaderboard("Repeated leaderboard page; snapshot not saved")
                    new_page = metadata.get("page")
                    if old_page is not None and new_page is not None and new_page != old_page + 1:
                        raise IncompleteLeaderboard("Leaderboard skipped or repeated a page")
                    seen.add(last_signature)
                    pages += 1
                return LeaderboardResult(
                    volumes=[v for r, v in sorted(rows.items()) if r > skip_top],
                    ranked_participants=len(rows), total_participants=total_joined, pages=pages)
            finally:
                if page is not None:
                    page.remove_listener("response", on_response)
                for task in tuple(tasks):
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tuple(tasks), return_exceptions=True)
                await self._close_ctx(ctx)


scraper = Scraper()
