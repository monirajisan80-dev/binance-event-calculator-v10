"""Scraper tests: parsers offline, plus a real headless-Chromium run against a local fake leaderboard."""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:dummy-test-token")
os.environ.setdefault("OWNER_ID", "111")
os.environ["REQUEST_DELAY_SEC"] = "0.3"

import scraper as S  # noqa: E402


def run(c):
    return asyncio.run(c)


@pytest.mark.parametrize("raw,expected", [
    ("2024-05-01 00:00", 1714521600),
    ("2024-05-01 00:00:00", 1714521600),
    ("2024/05/01", 1714521600),
    ("May 1, 2024", 1714521600),
    ("Sept 3rd, 2024 at 08:00", 1725350400),
    ("Jan. 5, 2024 12:00", 1704456000),
    ("not a date", None),
    ("", None),
])
def test_parse_date(raw, expected):
    assert S._parse_date(raw) == expected


def test_period_swaps_reversed_dates_and_handles_missing():
    assert S.parse_period("Promotion Period: 2024-06-10 00:00 to 2024-06-01 00:00") == (1717200000, 1717977600)
    assert S.parse_period("Activity Period: May 1, 2024 – May 8, 2024 (UTC)") == (1714521600, 1715126400)
    assert S.parse_period("No dates here") == (None, None)
    assert S.parse_period("Promotion Period: TBA") == (None, None)


def test_harvest_ignores_malformed_rows():
    rows = {}
    S.harvest_rows({"data": {"list": [
        {"rank": "1001", "volume": "$1,234.5"},
        {"rank": 1002, "volume": {"nested": 1}},
        {"rank": None, "volume": 5},
        {"rank": 1003, "tradeVolume": "nan"},
        {"rank": 1.5, "volume": 9},
        {"rank": True, "volume": 9},
        {"rankNo": 1005, "tradingVolume": 7},
        "junk", None, 42,
    ]}}, rows)
    assert rows == {1001: 1234.5, 1005: 7.0}
    deep = cur = {}
    for _ in range(50):
        cur["x"] = {}
        cur = cur["x"]
    S.harvest_rows(deep, rows)  # very deep JSON must not recurse forever


def _announce(text):
    sc = S.Scraper()
    sc._page_text = AsyncMock(return_value=(text, ["https://www.binance.com/en/activity/trading-competition/x"]))
    return run(sc.scrape_announcement("https://www.binance.com/en/support/announcement/x"))


def test_announcement_parses_pairs_pool_cap_and_dates():
    ev = _announce("Binance Spot Trading Tournament: Trade to Share 50,000 USDT\n"
                   "Promotion Period: 2024-05-01 00:00 (UTC) to 2024-05-08 00:00 (UTC)\n"
                   "Eligible pairs: ABC/USDT, ABC/FDUSD and xyz/usdt\n"
                   "50,000 USDT prize pool\nRules\nEach user can receive up to 300 USDT.\n"
                   "Users must trade at least $500 to qualify.")
    assert ev["pairs"] == ["ABC/FDUSD", "ABC/USDT"]
    assert ev["total_pool"] == 50000 and ev["pool_token"] == "USDT"
    assert (ev["reward_cap"], ev["reward_cap_token"]) == (300, "USDT")
    assert (ev["start_ts"], ev["end_ts"]) == (1714521600, 1715126400)
    assert ev["min_volume"] == 500
    assert ev["leaderboard_url"].endswith("/activity/trading-competition/x")


def test_announcement_with_unexpected_layout_does_not_crash():
    ev = _announce("Something completely different\nno numbers at all")
    assert ev["total_pool"] is None and ev["pairs"] == [] and ev["start_ts"] is None
    assert ev["reward_cap"] is None


def test_empty_announcement_raises_clear_error():
    with pytest.raises(ValueError):
        _announce("   \n  ")


# ---------- real browser against a fake Binance-like leaderboard ----------
PAGE = """<html><body><div id=t></div>
<button id=next aria-label="Next page">Next</button>
<script>
let p = 1; const last = 4;
async function load(){ const r = await fetch('/api/rows?page='+p); const j = await r.json();
  document.getElementById('t').innerText = 'page ' + p;
  if (p >= last) document.getElementById('next').disabled = true; }
document.getElementById('next').onclick = () => { p++; load(); };
fetch('/api/broken');   // malformed JSON must be ignored
fetch('/api/html');     // non-JSON must be ignored
load();
</script></body></html>"""


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/api/rows"):
            page = int(self.path.split("=")[1])
            # 500 rows per page: ranks 1..2000 over 4 pages, volume = rank
            rows = [{"rank": r, "volume": str(r)} for r in range((page - 1) * 500 + 1, page * 500 + 1)]
            return self._send(json.dumps({"data": {"list": rows, "currentPage": page, "totalPages": 4, "total": 2000}}), "application/json")
        if self.path == "/api/broken":
            return self._send("{not json", "application/json")
        if self.path == "/api/html":
            return self._send("<p>hi</p>", "text/html")
        return self._send(PAGE, "text/html")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def _browser_available():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


needs_browser = pytest.mark.skipif(not _browser_available(), reason="Chromium not installed")


@needs_browser
def test_real_leaderboard_pagination_and_rank_1001_volume(server):
    async def flow():
        sc = S.Scraper()
        await sc.start()
        try:
            vols = await sc.scrape_leaderboard(server, skip_top=1000)
            # ranks 1001..2000, volume == rank
            assert len(vols.volumes) == 1000
            assert vols.ranked_participants == 2000
            assert sum(vols.volumes) == sum(range(1001, 2001))
            # browser crash is recovered automatically
            await sc.browser.close()
            vols2 = await sc.scrape_leaderboard(server, skip_top=1000)
            assert len(vols2.volumes) == 1000
        finally:
            await sc.stop()
    run(flow())


@needs_browser
def test_unreachable_site_raises_without_leaking(server):
    async def flow():
        sc = S.Scraper()
        await sc.start()
        try:
            with pytest.raises(Exception):
                await sc.scrape_leaderboard("http://127.0.0.1:1/", skip_top=1000)
            assert len(sc.browser.contexts) == 0  # no leaked browser contexts
            text, _ = await sc._page_text(server)
            assert "page" in text or "Next" in text
            assert len(sc.browser.contexts) == 0
        finally:
            await sc.stop()
    run(flow())


@pytest.mark.parametrize('payload, expected', [
    ({'data': {'totalParticipants': '2,450', 'total': 2000}}, 2450),
    ({'total': 2000, 'pageCount': 200}, None),
    ({'joinedCount': 0}, 0),
    ({'participantCount': 'nan'}, None),
])
def test_joined_count_is_never_inferred_from_ranked_total(payload, expected):
    assert S.participant_total(payload) == expected


LONG_PAGE = """<html><body><div id=t></div><button aria-label="Next page" id=n>Next</button>
<script>
let p=1; const last=Number(new URLSearchParams(location.search).get('last')||205);
const mode=new URLSearchParams(location.search).get('mode')||'ok';
async function load(){
 const r=await fetch('/long-api?p='+p+'&last='+last+'&mode='+mode); const j=await r.json();
 document.getElementById('t').innerText='page '+p;
 document.getElementById('n').disabled=p>=last;
}
document.getElementById('n').onclick=()=>{p++;load()};load();
</script></body></html>"""


class LongFake(Fake):
    requested = []
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        if parsed.path == '/long-api':
            q = parse_qs(parsed.query); p = int(q['p'][0]); last = int(q['last'][0])
            mode = q['mode'][0]
            self.requested.append(p)
            if mode == 'slow' and p == 3:
                import time
                time.sleep(1)
            rows = [{'rank': (p-1)*10+i, 'volume': (p-1)*10+i} for i in range(1,11)]
            # Empty and duplicate pages still advance by the explicit page acknowledgement.
            if mode == 'empty' and 2 <= p <= 5:
                rows = []
            if mode == 'broken' and p == 3:
                rows[0]['volume'] = 'bad'
            if mode == 'skipped' and p == 3:
                p = 4
            total = last*10 - (40 if mode == 'empty' else 0)
            return self._send(json.dumps({'data': {'list': rows, 'currentPage': p,
                'totalPages': last, 'total': total, 'totalParticipants': total+100}}), 'application/json')
        return self._send(LONG_PAGE, 'text/html')


@pytest.fixture
def long_server():
    LongFake.requested = []
    srv = ThreadingHTTPServer(('127.0.0.1', 0), LongFake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_port}/'
    srv.shutdown(); srv.server_close()


@needs_browser
@pytest.mark.parametrize('mode,last', [('ok',205), ('slow',6), ('empty',7)])
def test_every_page_collected_without_200_page_or_stagnation_cutoff(long_server, monkeypatch, mode, last):
    monkeypatch.setattr(S, 'REQUEST_DELAY_SEC', 0)
    async def flow():
        sc = S.Scraper(); await sc.start()
        try:
            result = await sc.scrape_leaderboard(long_server+f'?mode={mode}&last={last}', 1000)
            assert LongFake.requested == list(range(1,last+1))
            assert result.pages == last
            assert result.ranked_participants == last*10-(40 if mode=='empty' else 0)
            assert result.total_participants == result.ranked_participants+100
            assert sum(result.volumes) == sum(range(1001,last*10+1))
            assert len(sc.browser.contexts) == 0
        finally:
            await sc.stop()
    run(flow())


@needs_browser
@pytest.mark.parametrize('mode', ['broken', 'skipped'])
def test_partial_or_skipped_page_is_failure_not_success(long_server, monkeypatch, mode):
    monkeypatch.setattr(S, 'REQUEST_DELAY_SEC', 0)
    async def flow():
        sc = S.Scraper(); await sc.start()
        try:
            with pytest.raises(S.IncompleteLeaderboard):
                await sc.scrape_leaderboard(long_server+f'?mode={mode}&last=4', 1000)
            assert len(sc.browser.contexts) == 0
        finally:
            await sc.stop()
    run(flow())


@needs_browser
def test_cancelled_scrape_cleans_up_browser(long_server, monkeypatch):
    monkeypatch.setattr(S, 'REQUEST_DELAY_SEC', 0)
    async def flow():
        sc = S.Scraper(); await sc.start()
        try:
            task = asyncio.create_task(sc.scrape_leaderboard(long_server+'?last=205', 1000))
            while len(LongFake.requested) < 3:
                await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(sc.browser.contexts) == 0
        finally:
            await sc.stop()
    run(flow())


REGRESSION_PAGE = '''<html><body><button aria-label="Next page" id=n disabled>Next</button>
<script>
let p=1; const mode=new URLSearchParams(location.search).get('mode');
async function load(){
 n.disabled=true; await (await fetch('/selected?p='+p+'&mode='+mode)).json();
 if(mode==='unrelated') await (await fetch('/other')).json();
 if(p<2 && mode==='delayed') setTimeout(()=>{n.disabled=false},700);
}
n.onclick=()=>{p++;load()};load();
</script></body></html>'''


class RegressionFake(Fake):
    requested = []
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed=urlparse(self.path)
        if parsed.path=='/other':
            return self._send(json.dumps({'data': {'list':[{'rank':1,'volume':999}], 'total':1,'totalPages':1,'currentPage':1,'totalParticipants':777}}),'application/json')
        if parsed.path=='/selected':
            q=parse_qs(parsed.query); p=int(q['p'][0]); mode=q['mode'][0]
            self.requested.append(p)
            data={'list':[{'rank':p,'volume':10*p}]}
            if mode=='malformed': data['list'].append({'rank':2,'volume':'bad'})
            if mode not in ('ambiguous','malformed'):
                data.update(currentPage=p,totalPages=2 if mode=='delayed' else 1,total=2 if mode=='delayed' else 1,totalParticipants=5)
            return self._send(json.dumps({'data':data}),'application/json')
        return self._send(REGRESSION_PAGE,'text/html')


@needs_browser
@pytest.mark.parametrize('mode', ['unrelated','delayed','malformed','ambiguous'])
def test_interception_and_terminal_integrity(mode, monkeypatch):
    monkeypatch.setattr(S,'REQUEST_DELAY_SEC',0)
    RegressionFake.requested=[]
    srv=ThreadingHTTPServer(('127.0.0.1',0),RegressionFake)
    threading.Thread(target=srv.serve_forever,daemon=True).start()
    async def flow():
        sc=S.Scraper();await sc.start()
        try:
            url=f'http://127.0.0.1:{srv.server_port}/?mode={mode}'
            if mode in ('malformed','ambiguous'):
                with pytest.raises(S.IncompleteLeaderboard):
                    await sc.scrape_leaderboard(url,0)
            else:
                result=await sc.scrape_leaderboard(url,0)
                assert result.volumes==([10,20] if mode=='delayed' else [10])
                assert result.total_participants==5
                assert RegressionFake.requested==([1,2] if mode=='delayed' else [1])
            assert len(sc.browser.contexts)==0
        finally:
            await sc.stop()
    try:
        run(flow())
    finally:
        srv.shutdown();srv.server_close()


# ---------- end-to-end journey: Stay Here popup, Main Reward Pool tab, usernames ----------
def test_season8_announcement_title_pool_period_tiers_and_join_now():
    sc = S.Scraper()
    sc._page_text = AsyncMock(return_value=(
        "Spot Trading Tournament Season 8\nMenu\nTotal Prize Pool: 400 BNB\n"
        "Promotion Period: 2026-10-20 00:00 (UTC) to 2026-11-03 23:59 (UTC)\n"
        "Rank\tReward\n1st\t40 BNB\n2nd - 10th\t10 BNB\n11 - 100\t1 BNB each\n",
        ["JOIN:https://www.binance.com/en/activity/trading-competition/season8",
         "https://www.binance.com/en/activity/other"]))
    ev = run(sc.scrape_announcement("https://www.binance.com/en/support/announcement/x"))
    assert ev["title"] == "Spot Trading Tournament Season 8"
    assert (ev["total_pool"], ev["pool_token"]) == (400, "BNB")
    assert (ev["start_ts"], ev["end_ts"]) == (1792454400, 1793750340)
    assert [(t["rank_from"], t["rank_to"], t["reward"]) for t in ev["reward_tiers"]] == [(1, 1, 40), (2, 10, 10), (11, 100, 1)]
    assert ev["leaderboard_url"].endswith("/trading-competition/season8")


JOURNEY_PAGE = """<html><body>
<div id=pop style="position:fixed;inset:0;background:#fff">View on Binance App
 <button id=stay>Stay Here</button><a href="https://example.com/app">Go to App</a></div>
<div role=tablist><button role=tab id=sub aria-selected=true>Sub Pool</button>
<button role=tab id=main>Main Reward Pool</button></div>
<button aria-label="Next page" id=n>Next</button>
<script>
let p=1, pool='sub'; const last=26;
stay.onclick=()=>pop.remove();
async function load(){ n.disabled=true;
 const j=await (await fetch('/j?pool='+pool+'&p='+p)).json(); n.disabled=p>=last; }
main.onclick=()=>{pool='main';p=1;load()};
n.onclick=()=>{p++;load()}; load();
</script></body></html>"""


class JourneyFake(Fake):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path == '/j':
            q = parse_qs(u.query); p = int(q['p'][0]); pool = q['pool'][0]
            base = 0 if pool == 'main' else 5000
            rows = [{'rank': (p-1)*10+i, 'nickName': f'{pool}{(p-1)*10+i}', 'volume': str(base+(p-1)*10+i)} for i in range(1, 11)]
            return self._send(json.dumps({'data': {'list': rows, 'currentPage': p, 'totalPages': 26, 'total': 260}}), 'application/json')
        return self._send(JOURNEY_PAGE, 'text/html')


@needs_browser
def test_stay_here_popup_main_pool_tab_and_every_page_with_usernames(monkeypatch):
    monkeypatch.setattr(S, 'REQUEST_DELAY_SEC', 0)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), JourneyFake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    async def flow():
        sc = S.Scraper(); await sc.start()
        try:
            res = await sc.scrape_leaderboard(f'http://127.0.0.1:{srv.server_port}/', 0)
            assert res.pages == 26 and res.ranked_participants == 260
            assert res.participants[0] == (1, 'main1', 1.0)
            assert res.participants[-1] == (260, 'main260', 260.0)
            assert [r for r, _, _ in res.participants] == list(range(1, 261))
        finally:
            await sc.stop()
    try:
        run(flow())
    finally:
        srv.shutdown(); srv.server_close()


@needs_browser
def test_stealth_context_hides_webdriver_and_uses_real_ua():
    async def flow():
        sc = S.Scraper(); await sc.start()
        try:
            ctx = await sc._new_context(); page = await ctx.new_page()
            await page.goto("data:text/html,<p>x</p>")
            assert await page.evaluate("navigator.webdriver") in (False, None)
            assert "HeadlessChrome" not in await page.evaluate("navigator.userAgent")
            assert (page.viewport_size["width"], page.viewport_size["height"]) in S.VIEWPORTS
            await ctx.close()
        finally:
            await sc.stop()
    run(flow())
