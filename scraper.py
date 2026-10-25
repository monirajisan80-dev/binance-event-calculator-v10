"""
Async scraping of Binance announcements + leaderboards.

Binance pages are JS-rendered, so we use one shared headless Chromium (Playwright).
For leaderboards we intercept the JSON XHR responses the page makes, which is far more
robust than parsing DOM. A semaphore limits concurrency; a delay is applied between requests.
"""
import asyncio
import logging
import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qsl
from playwright.async_api import async_playwright, Browser, TimeoutError as PWTimeout
from config import REQUEST_DELAY_SEC
try:  # playwright-stealth >= 2.0
    from playwright_stealth import Stealth
    _STEALTH = Stealth()
except Exception:  # package missing or old API: run without it rather than crash
    _STEALTH = None
from reward_caps import extract_reward_cap

log = logging.getLogger("scraper")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
VIEWPORTS = ((1920, 1080), (1536, 864), (1440, 900), (1366, 768))
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
               "--disable-setuid-sandbox", "--disable-dev-shm-usage"]


async def human_pause(lo: float = 0.6, hi: float = 1.8):
    """Randomised human-like wait. Disabled when REQUEST_DELAY_SEC is 0 (tests)."""
    if REQUEST_DELAY_SEC > 0:
        await asyncio.sleep(random.uniform(lo, hi))


async def human_wiggle(page):
    """Minor mouse movement + small scroll, like a person looking at the page."""
    if REQUEST_DELAY_SEC <= 0:
        return
    try:
        vp = page.viewport_size or {"width": 1366, "height": 768}
        await page.mouse.move(random.randint(100, vp["width"] - 100), random.randint(100, vp["height"] - 100),
                              steps=random.randint(8, 20))
        await page.mouse.wheel(0, random.randint(120, 480))
        await asyncio.sleep(random.uniform(0.3, 0.9))
        await page.mouse.wheel(0, -random.randint(60, 240))
    except Exception:
        log.debug("wiggle failed", exc_info=True)


async def human_click(page, locator, timeout: int = 10000):
    """Scroll the target into view, glide the mouse onto it, pause, then click."""
    if REQUEST_DELAY_SEC > 0:
        try:
            await locator.scroll_into_view_if_needed(timeout=timeout)
            box = await locator.bounding_box()
            if box:
                await page.mouse.move(box["x"] + box["width"] * random.uniform(0.3, 0.7),
                                      box["y"] + box["height"] * random.uniform(0.3, 0.7),
                                      steps=random.randint(10, 25))
            await asyncio.sleep(random.uniform(0.15, 0.6))
        except Exception:
            log.debug("human approach failed", exc_info=True)
    await locator.click(timeout=timeout)

MONTHS = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?"
          r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")
DATE_RE = re.compile(rf"(\d{{4}}[-/.]\d{{1,2}}[-/.]\d{{1,2}}(?:[ T]+\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?)"
                     rf"|((?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}(?:,?\s+(?:at\s+)?\d{{1,2}}:\d{{2}}(?::\d{{2}})?)?)", re.I)
PAIR_RE = re.compile(r"\b([A-Z0-9]{2,15})/(USDT|USDC|FDUSD|BTC|BNB|ETH|TRY|EUR)\b")
POOL_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(BNB|USDT|USDC|FDUSD)\b[^.\n]{0,40}(?:pool|reward|prize)", re.I)
MINVOL_RE = re.compile(r"(?:minimum|at least)[^$\n]{0,60}\$?\s*([\d,]+(?:\.\d+)?)\s*(?:USD|USDT)?", re.I)
PRIZE_RE = re.compile(r"(?:total\s+)?prize\s+pool(?:\s+of)?[:\s]*(?:up\s+to\s+)?(?:a\s+total\s+of\s+)?([\d,]+(?:\.\d+)?)\s*(BNB|USDT|USDC|FDUSD)\b"
                      r"|share\s+(?:a\s+)?(?:total\s+)?(?:prize\s+pool\s+of\s+)?(?:up\s+to\s+)?([\d,]+(?:\.\d+)?)\s*(BNB|USDT|USDC|FDUSD)\b", re.I)
TIER_RE = re.compile(r"^\s*(?:rank(?:ing)?\s*|top\s*)?#?(\d{1,6})(?:st|nd|rd|th)?"
                     r"(?:\s*(?:-|–|—|to|~)\s*#?(\d{1,6})(?:st|nd|rd|th)?)?\b[^\n]{0,80}?"
                     r"([\d,]+(?:\.\d+)?)\s*(BNB|USDT|USDC|FDUSD)\b", re.I)
STAY_HERE_RE = re.compile(r"^\s*stay\s+here\s*$", re.I)
JOIN_RE = re.compile(r"^\s*join\s+now\s*$", re.I)
MAIN_POOL_RE = re.compile(r"main\s+reward\s+pool", re.I)
NAME_KEYS = ("nickName", "nickname", "userName", "username", "displayName", "maskedName", "name", "uid", "userId")
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
    m = re.search(r"(?:Promotion|Activity|Campaign|Event|Tournament|Statistical|Statistics|Competition)\s+Period[:\s]*(.{0,200})", text or "", re.I | re.S)
    if not m:
        return None, None
    stamps = [t for t in (_parse_date(d[0] or d[1]) for d in DATE_RE.findall(m.group(1))) if t]
    start = stamps[0] if stamps else None
    end = stamps[1] if len(stamps) > 1 else None
    if start and end and end < start:
        start, end = end, start
    return start, end


def parse_reward_tiers(text: str) -> list[dict]:
    """Tier-wise reward breakdown, e.g. "1st  40 BNB" / "Rank 11 - 50  2 BNB each"."""
    tiers, seen = [], set()
    for line in (text or "").splitlines():
        if DATE_RE.search(line) or re.search(r"period|minimum|at least|\(utc\)", line, re.I):
            continue
        m = TIER_RE.match(line)
        if not m:
            continue
        lo = int(m.group(1)); hi = int(m.group(2)) if m.group(2) else lo
        if re.match(r"^\s*top\b", line, re.I) and not m.group(2):
            lo, hi = 1, int(m.group(1))
        amount = _to_float(m.group(3))
        if not amount or lo < 1 or hi < lo or (lo, hi) in seen:
            continue
        seen.add((lo, hi))
        tiers.append({"rank_from": lo, "rank_to": hi, "reward": amount,
                      "token": m.group(4).upper(), "label": " ".join(line.split())[:120]})
    return sorted(tiers, key=lambda t: t["rank_from"])


def _username(item: dict):
    for key in NAME_KEYS:
        value = item.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()[:100]
    return None


def harvest_rows(obj, rows: dict, depth: int = 0, names: dict | None = None):
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
                    if names is not None:
                        names[int(r)] = _username(it)
                    continue
            harvest_rows(it, rows, depth + 1, names)
    elif isinstance(obj, dict):
        for v in obj.values():
            harvest_rows(v, rows, depth + 1, names)


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
    # Every captured participant, rank 1 -> final: (rank, username, volume).
    participants: list[tuple[int, str | None, float]] = field(default_factory=list)


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
            headless=True, args=LAUNCH_ARGS, ignore_default_args=["--enable-automation"])

    async def _new_context(self):
        w, h = random.choice(VIEWPORTS)
        ctx = await self.browser.new_context(
            user_agent=UA, locale="en-US", viewport={"width": w, "height": h},
            screen={"width": w, "height": h}, device_scale_factor=1, timezone_id="UTC",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        if _STEALTH is not None:
            try:  # masks navigator.webdriver, plugins, languages, WebGL vendor, etc.
                await _STEALTH.apply_stealth_async(ctx)
            except Exception:
                log.warning("playwright-stealth could not be applied", exc_info=True)
        return ctx

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

    @staticmethod
    def _stay_here(page):
        return page.get_by_role("button", name=STAY_HERE_RE).or_(page.get_by_text(STAY_HERE_RE)).first

    async def _install_popup_guard(self, page):
        """Auto-dismiss the "View on Binance App" overlay (Stay Here / Go to App / Download).

        Never clicks "Go to App" or "Download" — that would leave the web view.
        Playwright runs the handler before any action whenever the overlay is visible.
        """
        async def handler(*_):
            try:
                await self._stay_here(page).click(timeout=3000)
            except Exception:
                log.debug("Stay Here click failed", exc_info=True)
        try:
            await page.add_locator_handler(self._stay_here(page), handler, no_wait_after=True)
        except Exception:
            log.debug("locator handler unsupported", exc_info=True)

    async def _dismiss_app_popup(self, page, wait_ms: int = 2500):
        """Explicit check right after load: the overlay appears immediately on open."""
        btn = self._stay_here(page)
        try:
            await btn.wait_for(state="visible", timeout=wait_ms)
        except PWTimeout:
            return False
        except Exception:
            return False
        try:
            await btn.click(timeout=5000)
            await btn.wait_for(state="hidden", timeout=5000)
        except Exception:
            log.debug("Stay Here dismissal incomplete", exc_info=True)
        return True

    async def _join_now_url(self, page) -> str | None:
        """Resolve where [Join Now] leads: its href, or the page/tab it opens when clicked."""
        btn = page.get_by_role("link", name=JOIN_RE).or_(page.get_by_role("button", name=JOIN_RE)) \
                  .or_(page.get_by_text(JOIN_RE)).first
        try:
            if await btn.count() == 0:
                return None
            href = await btn.evaluate("e => (e.closest('a') || e).href || null")
            if isinstance(href, str) and href.startswith("http"):
                return href
            before = page.url
            try:
                async with page.context.expect_page(timeout=8000) as new:
                    await human_click(page, btn, timeout=8000)
                target = await new.value
                await target.wait_for_load_state("domcontentloaded", timeout=30000)
                url = target.url
                await target.close()
                return url
            except PWTimeout:
                try:
                    await page.wait_for_url(lambda u: u != before, timeout=10000)
                except PWTimeout:
                    return None
                return page.url
        except Exception:
            log.debug("Join Now resolution failed", exc_info=True)
            return None

    async def _page_text(self, url: str) -> tuple[str, list[str]]:
        await self._ensure()
        async with self.sem:
            ctx = await self._new_context()
            try:
                page = await ctx.new_page()
                await self._install_popup_guard(page)
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await self._dismiss_app_popup(page)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass  # Binance keeps polling; DOM content is enough
                await page.wait_for_timeout(1500)
                await human_pause(1.5, 3.5)
                await human_wiggle(page)
                text = await page.inner_text("body", timeout=15000)
                try:  # put the article heading first so it wins title detection
                    h1 = (await page.locator("h1").first.inner_text(timeout=3000)).strip()
                    if h1:
                        text = h1 + "\n" + (text or "")
                except Exception:
                    pass
                try:
                    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
                except Exception:
                    hrefs = []
                hrefs = [h for h in hrefs if isinstance(h, str)]
                join = await self._join_now_url(page)
                if join and is_binance_url(join):
                    hrefs.insert(0, "JOIN:" + join)
                return text or "", hrefs
            finally:
                await self._close_ctx(ctx)
                await asyncio.sleep(REQUEST_DELAY_SEC)

    async def scrape_announcement(self, url: str) -> dict:
        if not is_binance_url(url):
            raise ValueError("Send a valid HTTPS Binance announcement link.")
        text, hrefs = await self._page_text(url)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        title = next((l for l in lines if len(l) > 15 and any(
            k in l.lower() for k in ("tournament", "season", "competition", "trade", "share", "campaign", "promotion", "earn"))), lines[0] if lines else url)

        if not lines:
            raise ValueError("Announcement page was empty or blocked.")
        start_ts, end_ts = parse_period(text)

        pairs = sorted({f"{a}/{b}" for a, b in PAIR_RE.findall(text)})
        pools = [(v, t.upper()) for a, t in POOL_RE.findall(text) if (v := _to_float(a))]
        for m in PRIZE_RE.finditer(text):
            amount, tok = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if (v := _to_float(amount)):
                pools.append((v, tok.upper()))
        total_pool, token = max(pools) if pools else (None, "BNB")
        mv = MINVOL_RE.search(text)
        min_volume = _to_float(mv.group(1)) if mv else None

        rules_idx = next((i for i, l in enumerate(lines) if "rule" in l.lower() or "eligib" in l.lower()), None)
        rules = "\n".join(lines[rules_idx:rules_idx + 6]) if rules_idx is not None else ""

        reward_cap, reward_cap_token = extract_reward_cap(text, token)
        join = next((h[5:] for h in hrefs if h.startswith("JOIN:")), None)
        hrefs = [h for h in hrefs if not h.startswith("JOIN:")]
        landing = join or next((h for h in hrefs if "/activity/" in h or "/campaign/" in h or "leaderboard" in h.lower()), None)
        return {
            "url": url, "title": title[:200], "start_ts": start_ts, "end_ts": end_ts,
            "pairs": pairs, "rules": rules[:1500], "min_volume": min_volume,
            "total_pool": total_pool, "remaining_pool": None, "pool_token": token,
            "fixed_slots": [], "leaderboard_url": landing,
            "reward_tiers": parse_reward_tiers(text),
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
        names: dict[int, str | None] = {}
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
                found, found_names = {}, {}
                harvest_rows(payload, found, names=found_names)
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
                    names.update(found_names)
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
            ctx = await self._new_context()
            page = None
            try:
                page = await ctx.new_page()
                page.on("response", on_response)
                await self._install_popup_guard(page)
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Mobile overlay "View on Binance App": always choose Stay Here.
                await self._dismiss_app_popup(page)
                await human_pause(1.5, 3.5)
                await human_wiggle(page)
                tab = page.get_by_role("tab", name=MAIN_POOL_RE).or_(page.get_by_text(MAIN_POOL_RE)).first
                has_tab = False
                try:
                    await tab.wait_for(state="visible", timeout=8000)
                    has_tab = True
                except Exception:
                    pass
                if has_tab:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except PWTimeout:
                        pass
                    # Discard whatever the default tab loaded; the snapshot is Main Reward Pool only.
                    rows.clear(); names.clear(); metadata.clear()
                    source_identity = last_signature = total_joined = capture_error = None
                    expected_page = 1
                    previous = revision
                    await human_click(page, tab)
                    try:
                        await wait_advance(previous)
                    except IncompleteLeaderboard:
                        if not rows:  # tab was already selected and fired no new request
                            await page.reload(wait_until="domcontentloaded", timeout=60000)
                            await self._dismiss_app_popup(page)
                            previous = revision
                            await human_click(page, tab)
                            await wait_advance(previous)
                        else:
                            raise
                else:
                    await wait_advance(0)
                pages = 1
                seen = {last_signature}
                while True:
                    # Let loading/disabled states settle AFTER response parsing.
                    await asyncio.sleep(max(REQUEST_DELAY_SEC, 0.1))
                    await human_pause(0.4, 1.4)
                    nxt = page.locator("button[aria-label*='next' i], [role='button'][aria-label*='next' i], "
                                       ".bn-pagination-next, li[title='Next Page'], [data-testid*='next' i]").first
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
                    await human_wiggle(page) if random.random() < 0.15 else None
                    await human_click(page, nxt)
                    await wait_advance(previous)
                    if last_signature in seen:
                        raise IncompleteLeaderboard("Repeated leaderboard page; snapshot not saved")
                    new_page = metadata.get("page")
                    if old_page is not None and new_page is not None and new_page != old_page + 1:
                        raise IncompleteLeaderboard("Leaderboard skipped or repeated a page")
                    seen.add(last_signature)
                    pages += 1
                ranks = sorted(rows)
                if ranks and ranks != list(range(1, len(ranks) + 1)):
                    log.warning("Leaderboard source has rank gaps (%d rows, max rank %d)", len(ranks), ranks[-1])
                return LeaderboardResult(
                    volumes=[rows[r] for r in ranks if r > skip_top],
                    ranked_participants=len(rows), total_participants=total_joined, pages=pages,
                    participants=[(r, names.get(r), rows[r]) for r in ranks])
            finally:
                if page is not None:
                    page.remove_listener("response", on_response)
                for task in tuple(tasks):
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tuple(tasks), return_exceptions=True)
                await self._close_ctx(ctx)


scraper = Scraper()
