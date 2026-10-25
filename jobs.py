import asyncio
import logging
from weakref import WeakValueDictionary
from calc import remaining_pool_stats
from config import SKIP_TOP, FALLBACK_REWARD_CAP, FALLBACK_REWARD_TOKEN, CLEANUP_DAYS, REQUEST_DELAY_SEC
from db import db
from reward_caps import effective_reward_cap
from scraper import scraper

log = logging.getLogger("jobs")
_event_locks = WeakValueDictionary()
_scrape_lock = asyncio.Lock()   # mutex: never overlap scrape cycles


async def ingest_link(url: str) -> int:
    ev = await scraper.scrape_announcement(url)
    eid = await db.upsert_event(ev)
    log.info("Ingested event %s: %s", eid, ev["title"])
    return eid


async def refresh_event(ev: dict):
    eid = ev["id"]
    lock = _event_locks.setdefault(eid, asyncio.Lock())
    async with lock:
        current = await db.get_event(eid)
        if not current or not current.get("leaderboard_url"):
            return False
        result = await scraper.scrape_leaderboard(current["leaderboard_url"], SKIP_TOP)
        stats = build_stats(current, result.volumes)
        stats.update(total_participants=result.total_participants,
                     ranked_participants=result.ranked_participants,
                     leaderboard_pages=result.pages)
        if not await db.update_stats(eid, stats, current["settings_version"]):
            raise RuntimeError("Event settings changed during scraping; previous snapshot retained. Retry refresh.")
        return True


def build_stats(ev: dict, vols: list[float]) -> dict:
    pool = float(ev.get("remaining_pool") or 0)
    cap, source = effective_reward_cap(ev, FALLBACK_REWARD_CAP, FALLBACK_REWARD_TOKEN)
    stats = remaining_pool_stats(vols, pool, cap if cap is not None else 0.0).to_dict()
    stats["cap_source"] = source if cap is not None else None
    stats["has_estimate"] = pool > 0 and cap is not None and stats["per_dollar"] > 0
    return stats


async def scrape_cycle() -> str:
    if _scrape_lock.locked():
        log.info("Previous scrape still running; skipping this tick")
        return "skipped (already running)"
    async with _scrape_lock:
        events = await db.list_events(live_only=True)
        ok = 0
        for ev in events:
            try:
                await refresh_event(ev)
                ok += 1
            except Exception:
                log.exception("Refresh failed for event %s", ev["id"])
            await asyncio.sleep(REQUEST_DELAY_SEC)
        return f"refreshed {ok}/{len(events)}"


async def job_scrape(context):
    try:
        await scrape_cycle()
    except Exception:
        log.exception("scrape job crashed")


async def job_cleanup(context):
    try:
        n = await db.purge_old(CLEANUP_DAYS)
        if n:
            log.info("Purged %d old events", n)
    except Exception:
        log.exception("cleanup job crashed")
