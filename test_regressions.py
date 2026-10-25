"""v8 snapshot, parsing and lifecycle regressions; no production credentials."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from test_admin import store, event, run
import jobs
import main
import scraper as S
from reward_caps import extract_reward_cap


def test_reingestion_and_failed_scrape_retain_snapshot(store, monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs, 'db', store)
        eid = await store.upsert_event(event())
        saved = {'total_volume': 12450000, 'ranked_participants': 2450}
        await store.update_stats(eid, saved)
        before = await store.get_event(eid)
        await store.upsert_event(event())
        await store.set_field(eid, 'leaderboard_url', 'https://www.binance.com/lb')
        monkeypatch.setattr(jobs.scraper, 'scrape_leaderboard', AsyncMock(side_effect=S.IncompleteLeaderboard('bad page')))
        with pytest.raises(S.IncompleteLeaderboard):
            await jobs.refresh_event(await store.get_event(eid))
        after = await store.get_event(eid)
        assert json.loads(after['last_stats']) == saved
        assert after['last_scraped'] == before['last_scraped']
        assert after['stats_stale']
    run(flow())


def test_settings_change_during_scrape_cannot_publish_old_estimate(store, monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs, 'db', store)
        ev = event(); ev['leaderboard_url'] = 'https://www.binance.com/lb'
        eid = await store.upsert_event(ev)
        await store.update_stats(eid, {'total_volume': 12345})
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            started.set(); await release.wait()
            return S.LeaderboardResult([1000], 1001)
        monkeypatch.setattr(jobs.scraper, 'scrape_leaderboard', delayed)
        old = asyncio.create_task(jobs.refresh_event(await store.get_event(eid)))
        await started.wait()
        await store.set_field(eid, 'remaining_pool', 80)
        release.set()
        with pytest.raises(RuntimeError, match='settings changed'):
            await old
        assert json.loads((await store.get_event(eid))['last_stats']) == {'total_volume': 12345}
        monkeypatch.setattr(jobs.scraper, 'scrape_leaderboard', AsyncMock(return_value=S.LeaderboardResult([1000], 1001)))
        await jobs.refresh_event(await store.get_event(eid))
        ev = await store.get_event(eid)
        assert json.loads(ev['last_stats'])['per_dollar'] == 80 / 1000
        assert not ev['stats_stale']
    run(flow())


def test_simultaneous_refreshes_are_serialized(store, monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs, 'db', store)
        ev = event(); ev['leaderboard_url'] = 'https://www.binance.com/lb'
        eid = await store.upsert_event(ev)
        active = peak = 0
        async def scrape(*args):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
            return S.LeaderboardResult([1000], 1001)
        monkeypatch.setattr(jobs.scraper, 'scrape_leaderboard', scrape)
        ev = await store.get_event(eid)
        await asyncio.gather(jobs.refresh_event(ev), jobs.refresh_event(ev))
        assert peak == 1
    run(flow())


@pytest.mark.parametrize('text', ['Each user can trade up to 100 BNB.', 'Each user can deposit up to 100 BNB.', 'Maximum purchase of 100 BNB per user.', 'Cap of 100 BNB per account.'])
def test_nonreward_limits_not_reward_caps(text):
    assert extract_reward_cap(text, 'BNB') == (None, None)


def test_multiline_period():
    assert S.parse_period('Promotion Period:\n2024-05-01 00:00 (UTC) to\n2024-05-08 00:00 (UTC)') == (1714521600,1715126400)


def test_health_listener_closes_and_port_can_be_rebound(monkeypatch):
    async def flow():
        monkeypatch.setattr(main, 'PORT', 0)
        runner = await main.health_server()
        site = next(iter(runner.sites))
        port = site._server.sockets[0].getsockname()[1]
        await runner.cleanup()
        listener = await asyncio.start_server(lambda r,w: w.close(), '0.0.0.0', port)
        listener.close(); await listener.wait_closed()
    run(flow())


@pytest.mark.parametrize('failed_stage', ['db', 'scraper', 'health', 'initialize', 'app', 'polling', 'monitor', None])
def test_startup_and_shutdown_cleanup_at_every_stage(monkeypatch, failed_stage):
    async def flow():
        closed = []
        def start(stage):
            async def call():
                if stage == failed_stage:
                    raise RuntimeError(stage)
            return call
        def close(stage, fails=False):
            async def call():
                closed.append(stage)
                if fails:
                    raise RuntimeError('cleanup')
            return call
        database = SimpleNamespace(init=start('db'), close=close('db'))
        browser = SimpleNamespace(start=start('scraper'), stop=close('scraper', True))
        app = Mock()
        app.running = False
        updater = SimpleNamespace(running=False)
        async def start_app():
            await start('app')(); app.running = True
        async def start_polling(**kwargs):
            await start('polling')(); updater.running = True
        updater.start_polling = start_polling; updater.stop = close('polling')
        app.updater = updater; app.initialize = start('initialize')
        app.start = start_app; app.stop = close('app'); app.shutdown = close('shutdown')
        builder = Mock(); builder.token.return_value = builder
        builder.concurrent_updates.return_value = builder; builder.build.return_value = app
        monkeypatch.setattr(main, 'Application', SimpleNamespace(builder=lambda: builder))
        monkeypatch.setattr(main, 'db', database); monkeypatch.setattr(main, 'scraper', browser)
        monkeypatch.setattr(main, 'HEALTH_SERVER', True)
        async def health():
            await start('health')()
            return SimpleNamespace(cleanup=close('health'))
        monkeypatch.setattr(main, 'health_server', health)
        monkeypatch.setattr(main, 'Monitor', lambda notify: SimpleNamespace(start=start('monitor'), stop=close('monitor')))
        stop = asyncio.Event(); stop.set()
        if failed_stage:
            with pytest.raises(RuntimeError, match=failed_stage):
                await main.main(stop)
        else:
            await main.main(stop)
        stages = ['db','scraper','health','initialize','app','polling','monitor']
        index = stages.index(failed_stage) if failed_stage else len(stages)
        expected = ['db']
        if index >= 1: expected.append('scraper')
        if index >= 3: expected.extend(['health','shutdown'])
        if index >= 5: expected.append('app')
        if index >= 6: expected.append('polling')
        if index >= 6: expected.append('monitor')
        assert sorted(closed) == sorted(expected)
        assert len(closed) == len(set(closed))
    run(flow())


@pytest.mark.parametrize('url', ['https://example.com/?binance.com', 'https://binance.com.example.org/x', 'https://binance.com@127.0.0.1/x', 'http://www.binance.com/x', 'https://www.binance.com:8080/x'])
def test_announcement_rejects_imitation_binance_addresses(url):
    sc = S.Scraper()
    sc._page_text = AsyncMock()
    with pytest.raises(ValueError):
        run(sc.scrape_announcement(url))
    sc._page_text.assert_not_awaited()
