"""Dynamic monitoring, migration, and event reward-cap regression tests."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from test_admin import store, run, update, event
import handlers
import jobs
import monitor
from reward_caps import extract_reward_cap, effective_reward_cap
from calc import user_reward, remaining_pool_stats
from scraper import LeaderboardResult, IncompleteLeaderboard

@pytest.mark.parametrize('actor', [111])
@pytest.mark.parametrize('kind,open_button,add_button,value', [
    ('channel','o:chan','o:chadd','@second_channel'),
    ('keyword','o:kw','o:kwadd','New campaign'),
])
def test_owner_ui_settings_flow(store, actor, kind, open_button, add_button, value):
    async def flow():
        await store.add_admin(111,222)
        ctx = SimpleNamespace(user_data={})
        await handlers.admin(update(actor),ctx)
        await handlers.on_callback(update(actor,open_button),ctx)
        await handlers.on_callback(update(actor,add_button),ctx)
        await handlers.on_text(update(actor,text=value),ctx)
        expected = value.lstrip('@')
        saved = await store.monitoring_items(kind)
        item = next(r for r in saved if r['value']==expected)
        await store.close(); await store.init()
        assert item in await store.monitoring_items(kind)
        viewed = update(actor,open_button)
        await handlers.on_callback(viewed,ctx)
        buttons = viewed.callback_query.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard
        callback = next(b.callback_data for row in buttons for b in row if b.callback_data.endswith(':'+str(item['id'])))
        await handlers.on_callback(update(actor,callback),ctx)
        assert item not in await store.monitoring_items(kind)
    run(flow())

@pytest.mark.parametrize('kind,value', [('channel','@blocked_channel'),('keyword','blocked')])
def test_non_admin_and_revoked_pending_settings_denied(store, kind, value):
    with pytest.raises(PermissionError): run(store.add_monitoring_item(333,kind,value))
    run(store.add_admin(111,222))
    ctx = SimpleNamespace(user_data={'await':('setting_add',kind)})
    run(store.remove_admin(111,222))
    run(handlers.on_text(update(222,text=value),ctx))
    assert value.lstrip('@') not in [r['value'] for r in run(store.monitoring_items(kind))]

@pytest.mark.parametrize('kind', ['channel','keyword'])
def test_empty_saved_lists_not_reseeded(store,kind):
    for item in run(store.monitoring_items(kind)):
        run(store.remove_monitoring_item(111,kind,item['id']))
    run(store.close());run(store.init())
    assert run(store.monitoring_items(kind)) == []

def test_stale_keyword_removal_does_not_delete_other_item(store):
    run(store.add_monitoring_item(111,'keyword','First'))
    run(store.add_monitoring_item(111,'keyword','Second'))
    items=run(store.monitoring_items('keyword'))
    first=next(r for r in items if r['value']=='First')
    run(store.remove_monitoring_item(111,'keyword',first['id']))
    run(handlers.on_callback(update(111,f"o:kwdel:{first['id']}"),SimpleNamespace(user_data={})))
    assert 'Second' in run(store.keywords())

@pytest.mark.parametrize('value',['','   ','t.me/+invite','@bad/name','-123','https://example.com/channel'])
def test_invalid_channel_rejected(store,value):
    with pytest.raises(ValueError): run(store.add_monitoring_item(111,'channel',value))

def test_duplicates_are_case_insensitive(store):
    assert run(store.add_monitoring_item(111,'keyword','Example'))
    assert not run(store.add_monitoring_item(111,'keyword','EXAMPLE'))
    assert run(store.add_monitoring_item(111,'channel','https://t.me/Another_Channel/'))
    assert not run(store.add_monitoring_item(111,'channel','@another_channel'))

def test_legacy_lists_migrate_once(store):
    async def flow():
        await store.set_setting('channel','@legacy_channel')
        await store.set_setting('keywords',json.dumps(['Legacy']))
        await store.pool.execute('DELETE FROM monitoring_items')
        await store.pool.execute("DELETE FROM settings WHERE key='monitoring_items_initialized'")
        await store.close();await store.init()
        assert await store.channels()==['legacy_channel']
        assert await store.keywords()==['Legacy']
    run(flow())

@pytest.mark.parametrize('text,token,cap', [
    ('Each user can receive up to 0.1 BNB.','BNB',0.1),
    ('Rewards per user are capped at 0.075 BNB.','BNB',0.075),
    ('Maximum reward of 1,250 USDT per participant.','USDT',1250),
    ('Reward cap: 0.2 BNB per user.','BNB',0.2),
    ('Each eligible participant can earn a maximum of 25 USDT.','USDT',25),
    ('The reward for each user shall not exceed 0.05 BNB.','BNB',0.05),
    ('Total prize pool 100 BNB. Minimum trading volume 50 USDT.','BNB',None),
    ('Each user receives up to 50 USDT.','BNB',None),
    ('Each user receives up to 0.1 BNB. Each user receives up to 0.2 BNB.','BNB',None),
    ('Each user receives up to 0 BNB.','BNB',None),
])
def test_cap_extraction(text,token,cap):
    assert extract_reward_cap(text,token)==(cap,token if cap is not None else None)

def test_fallback_is_token_aware():
    assert effective_reward_cap({'pool_token':'BNB'},0.05,'BNB')==(0.05,'fallback')
    assert effective_reward_cap({'pool_token':'USDT'},0.05,'BNB')==(None,'unavailable')

@pytest.mark.parametrize('cap', [0.1,0.25])
def test_refresh_uses_event_specific_cap(store,monkeypatch,cap):
    async def flow():
        ev=event(reward_cap=cap,reward_cap_token='BNB')
        ev['leaderboard_url']='https://www.binance.com/activity/test'
        eid=await store.upsert_event(ev)
        monkeypatch.setattr(jobs,'db',store)
        monkeypatch.setattr(jobs.scraper,'scrape_leaderboard',AsyncMock(return_value=LeaderboardResult([1000,2000], 1002)))
        await jobs.refresh_event(await store.get_event(eid))
        stats=json.loads((await store.get_event(eid))['last_stats'])
        assert stats['cap']==cap
        assert stats['cap_source']=='announcement'
        assert user_reward(100000,remaining_pool_stats([1000,2000],40,cap))==cap
        await store.close();await store.init()
        assert (await store.get_event(eid))['reward_cap']==cap
    run(flow())

def test_scraper_stores_extracted_cap(monkeypatch):
    from scraper import Scraper
    scraper=Scraper()
    monkeypatch.setattr(scraper,'_page_text',AsyncMock(return_value=(
        'Trade to share\n100 BNB prize pool\nRules\nEach user can receive up to 0.15 BNB.',[])))
    ev=run(scraper.scrape_announcement('https://www.binance.com/test'))
    assert ev['reward_cap']==0.15
    assert ev['reward_cap_token']=='BNB'

def test_refresh_fallback_and_foreign_token(store,monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs,'db',store)
        monkeypatch.setattr(jobs,'FALLBACK_REWARD_CAP',0.05)
        monkeypatch.setattr(jobs,'FALLBACK_REWARD_TOKEN','BNB')
        mock=AsyncMock(return_value=LeaderboardResult([1000], 1001))
        monkeypatch.setattr(jobs.scraper,'scrape_leaderboard',mock)
        ev=event();ev['leaderboard_url']='https://www.binance.com/activity/test'
        eid=await store.upsert_event(ev)
        await jobs.refresh_event(await store.get_event(eid))
        stats=json.loads((await store.get_event(eid))['last_stats'])
        assert stats['cap']==0.05 and stats['cap_source']=='fallback'
        ev.update(pool_token='USDT')
        await store.upsert_event(ev)
        mock.reset_mock()
        await jobs.refresh_event(await store.get_event(eid))
        mock.assert_awaited()  # volume still refreshes without a compatible cap
        stats=json.loads((await store.get_event(eid))['last_stats'])
        assert stats['total_volume']==1000 and stats['has_estimate'] is False
    run(flow())

def test_volume_refreshes_without_pool_and_shows_on_card(store,monkeypatch):
    import ui
    async def flow():
        monkeypatch.setattr(jobs,'db',store)
        monkeypatch.setattr(jobs.scraper,'scrape_leaderboard',AsyncMock(return_value=LeaderboardResult([12_000_000,450_000], 1002)))
        ev=event();ev.update(leaderboard_url='https://www.binance.com/lb',remaining_pool=None)
        eid=await store.upsert_event(ev)
        await jobs.refresh_event(await store.get_event(eid))
        text=ui.event_text(await store.get_event(eid))
        assert 'Total Volume (Rank 1001+): $12,450,000' in text
    run(flow())

def test_rescrape_keeps_manual_pool_and_custom_title(store):
    async def flow():
        ev=event();eid=await store.upsert_event(ev)
        await store.set_field(eid,'remaining_pool',55.0)
        await store.set_event_title(handlers.OWNER_ID,eid,'My title')
        ev.update(remaining_pool=None,title='Scraped title')
        await store.upsert_event(ev)
        e=await store.get_event(eid)
        assert e['remaining_pool']==55.0 and e['title']=='My title'
    run(flow())

def test_monitor_tracks_dynamic_changes(store,monkeypatch):
    async def flow():
        monkeypatch.setattr(monitor,'db',store)
        ingest=AsyncMock(return_value=7)
        monkeypatch.setattr(monitor,'ingest_link',ingest)
        bot=monitor.Monitor(AsyncMock())
        for name in ['alpha_channel','beta_channel']:
            await store.add_monitoring_item(111,'channel',name)
        await store.add_monitoring_item(111,'keyword','trackme')
        msg=SimpleNamespace(entities=[],reply_markup=None)
        for name in ['alpha_channel','beta_channel','unknown_channel']:
            e=SimpleNamespace(is_channel=True,get_chat=AsyncMock(return_value=SimpleNamespace(username=name)),
                              chat_id=-10012345,raw_text='TRACKME https://www.binance.com/en/support/announcement/test',message=msg)
            await bot.handle_message(e)
        assert ingest.await_count==2
        beta=next(r for r in await store.monitoring_items('channel') if r['value']=='beta_channel')
        await store.remove_monitoring_item(111,'channel',beta['id'])
        e.get_chat=AsyncMock(return_value=SimpleNamespace(username='beta_channel'))
        await bot.handle_message(e)
        assert ingest.await_count==2
        await store.add_monitoring_item(111,'channel','-10012345')
        await bot.handle_message(e)
        assert ingest.await_count==3
    run(flow())

def test_monitor_subscribes_added_channels(store,monkeypatch):
    async def flow():
        monkeypatch.setattr(monitor,'db',store)
        bot=monitor.Monitor(AsyncMock())
        bot.client=AsyncMock()
        bot.client.get_entity=AsyncMock(return_value=object())
        monkeypatch.setattr(monitor,'JoinChannelRequest',lambda entity: entity)
        await bot.sync_channels()
        before=bot.client.await_count
        await store.add_monitoring_item(111,'channel','new_channel')
        await bot.sync_channels()
        assert bot.client.await_count==before+1
        await bot.sync_channels()
        assert bot.client.await_count==before+1
    run(flow())


def test_reward_cap_columns_migrate_and_parse_saved_rules(store):
    from test_admin import raw_sql
    run(store.close())
    run(raw_sql("ALTER TABLE events DROP COLUMN reward_cap, DROP COLUMN reward_cap_token"))
    run(raw_sql("INSERT INTO events(url,title,rules,pool_token,created_at) VALUES($1,$2,$3,$4,$5)",
                "https://www.binance.com/legacy", "Old", "Each user earns up to 0.12 BNB.", "BNB", 1))
    run(store.init())
    assert run(store.list_events())[0]['reward_cap']==0.12


def test_cap_updates_on_reingestion_without_resetting_title(store):
    eid=run(store.upsert_event(event(reward_cap=0.1,reward_cap_token='BNB')))
    run(store.set_event_title(111,eid,'Custom'))
    run(store.update_stats(eid,{'cap':0.1}))
    run(store.upsert_event(event(reward_cap=0.2,reward_cap_token='BNB')))
    ev=run(store.get_event(eid))
    assert ev['reward_cap']==0.2
    assert json.loads(ev['last_stats']) == {'cap':0.1}
    assert ev['stats_stale'] is True
    assert ev['title']=='Custom'

@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "abc", ""])
def test_pool_input_rejects_bad_numbers(bad):
    with pytest.raises(ValueError):
        handlers.parse_amount(bad)

def test_pool_input_accepts_formatted_numbers():
    assert handlers.parse_amount("12,500") == 12500 and handlers.parse_amount("$40") == 40

@pytest.mark.parametrize("bad", ["x 10", "5-2 10", "1-1", "0-3 5", ""])
def test_fixed_slots_reject_malformed_lines(bad):
    with pytest.raises(ValueError):
        handlers._parse_slots(bad)


def test_participant_counts_saved_separately_and_survive_restart(store, monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs, 'db', store)
        monkeypatch.setattr(jobs.scraper, 'scrape_leaderboard', AsyncMock(
            return_value=LeaderboardResult([10, 20], 1002, 2450, 201)))
        ev = event(); ev['leaderboard_url'] = 'https://www.binance.com/activity/test'
        eid = await store.upsert_event(ev)
        await jobs.refresh_event(await store.get_event(eid))
        await store.close(); await store.init()
        stats = json.loads((await store.get_event(eid))['last_stats'])
        assert stats['total_participants'] == 2450
        assert stats['ranked_participants'] == 1002
        assert stats['participants'] == 2
        assert stats['leaderboard_pages'] == 201
    run(flow())


def test_failed_pagination_preserves_last_complete_snapshot(store, monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs, 'db', store)
        ev = event(); ev['leaderboard_url'] = 'https://www.binance.com/activity/test'
        eid = await store.upsert_event(ev)
        await store.update_stats(eid, {'total_volume': 12345, 'ranked_participants': 2000})
        before = await store.get_event(eid)
        monkeypatch.setattr(jobs.scraper, 'scrape_leaderboard', AsyncMock(
            side_effect=IncompleteLeaderboard('failed page')))
        with pytest.raises(IncompleteLeaderboard):
            await jobs.refresh_event(before)
        after = await store.get_event(eid)
        assert after['last_stats'] == before['last_stats']
        assert after['last_scraped'] == before['last_scraped']
    run(flow())


def test_complete_top_1000_only_refreshes_zero_tail(store, monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs, 'db', store)
        monkeypatch.setattr(jobs.scraper, 'scrape_leaderboard', AsyncMock(
            return_value=LeaderboardResult([], 500)))
        ev = event(); ev['leaderboard_url'] = 'https://www.binance.com/activity/test'
        eid = await store.upsert_event(ev)
        await jobs.refresh_event(await store.get_event(eid))
        stats = json.loads((await store.get_event(eid))['last_stats'])
        assert stats['total_volume'] == 0
        assert stats['ranked_participants'] == 500
        assert stats['total_participants'] is None
    run(flow())



def test_scrape_cycle_has_no_whole_event_timeout(store, monkeypatch):
    async def flow():
        monkeypatch.setattr(jobs, 'db', store)
        await store.upsert_event(event())
        refreshed = AsyncMock()
        monkeypatch.setattr(jobs, 'refresh_event', refreshed)
        monkeypatch.setattr(jobs, 'REQUEST_DELAY_SEC', 0)
        # A whole-event wait_for would reintroduce the premature-cutoff bug.
        def forbidden_timeout(*args, **kwargs):
            raise AssertionError('whole event timeout is forbidden')
        monkeypatch.setattr(jobs.asyncio, 'wait_for', forbidden_timeout)
        assert await jobs.scrape_cycle() == 'refreshed 1/1'
        refreshed.assert_awaited_once()
    run(flow())
