"""PostgreSQL (Supabase) data layer using asyncpg."""
import json
import logging
import re
from datetime import datetime, timezone, timedelta

import asyncpg

from config import DATABASE_URL, DB_POOL_MIN, DB_POOL_MAX, DEFAULT_CHANNELS, DEFAULT_KEYWORDS, OWNER_ID
from reward_caps import extract_reward_cap

log = logging.getLogger("db")

# Telegram IDs and unix timestamps exceed 32-bit, so they are BIGINT.
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitoring_items (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('channel', 'keyword')),
  value TEXT NOT NULL,
  normalized TEXT NOT NULL,
  UNIQUE(kind, normalized)
);
CREATE TABLE IF NOT EXISTS admins (
  user_id BIGINT PRIMARY KEY CHECK(user_id > 0),
  appointed_by BIGINT NOT NULL,
  created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  url TEXT UNIQUE NOT NULL,
  title TEXT,
  custom_title TEXT,
  reward_cap DOUBLE PRECISION,
  reward_cap_token TEXT,
  start_ts BIGINT,
  end_ts BIGINT,
  pairs TEXT DEFAULT '[]',
  rules TEXT DEFAULT '',
  min_volume DOUBLE PRECISION,
  total_pool DOUBLE PRECISION,
  remaining_pool DOUBLE PRECISION,
  pool_token TEXT DEFAULT 'BNB',
  fixed_slots TEXT DEFAULT '[]',
  leaderboard_url TEXT,
  settings_version INTEGER NOT NULL DEFAULT 0,
  stats_version INTEGER,
  last_stats TEXT,
  last_scraped BIGINT,
  created_at BIGINT NOT NULL
);
ALTER TABLE events ADD COLUMN IF NOT EXISTS custom_title TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS reward_cap DOUBLE PRECISION;
ALTER TABLE events ADD COLUMN IF NOT EXISTS reward_cap_token TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS settings_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN IF NOT EXISTS stats_version INTEGER;
"""

_INIT_LOCK_KEY = 815_042_201  # advisory lock so two instances never migrate at once


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _rowcount(status: str) -> int:
    """asyncpg returns e.g. 'INSERT 0 1' / 'UPDATE 3' / 'DELETE 0'."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


class DB:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def init(self):
        dsn = self.dsn or DATABASE_URL
        if not dsn:
            raise RuntimeError("DATABASE_URL is not set")
        # statement_cache_size=0 keeps asyncpg compatible with Supabase's
        # transaction pooler (PgBouncer, port 6543) as well as direct connections.
        self.pool = await asyncpg.create_pool(dsn, min_size=DB_POOL_MIN, max_size=DB_POOL_MAX,
                                              statement_cache_size=0, command_timeout=30)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _INIT_LOCK_KEY)
            await conn.execute(SCHEMA)
            if await conn.fetchval("SELECT value FROM settings WHERE key='monitoring_items_initialized'") is None:
                old_channel = await conn.fetchval("SELECT value FROM settings WHERE key='channel'")
                channels = [old_channel] if old_channel is not None else DEFAULT_CHANNELS
                old_keywords = await conn.fetchval("SELECT value FROM settings WHERE key='keywords'")
                keywords = json.loads(old_keywords) if old_keywords is not None else DEFAULT_KEYWORDS
                for kind, values in (("channel", channels), ("keyword", keywords)):
                    for value in values:
                        if not value.strip():
                            continue
                        try:
                            value = self.normalize_channel(value) if kind == "channel" else value.strip()
                        except ValueError:
                            continue
                        await conn.execute(
                            "INSERT INTO monitoring_items(kind,value,normalized) VALUES($1,$2,$3) "
                            "ON CONFLICT (kind, normalized) DO NOTHING", kind, value, value.casefold())
                await conn.execute("INSERT INTO settings(key,value) VALUES('monitoring_items_initialized','1') "
                                   "ON CONFLICT (key) DO NOTHING")
            for row in await conn.fetch("SELECT id,rules,pool_token FROM events WHERE reward_cap IS NULL"):
                cap, token = extract_reward_cap(row["rules"], row["pool_token"] or "BNB")
                if cap is not None:
                    await conn.execute("UPDATE events SET reward_cap=$1,reward_cap_token=$2 WHERE id=$3",
                                       cap, token, row["id"])

    async def ping(self) -> bool:
        return self.pool is not None and await self.pool.fetchval("SELECT 1") == 1

    # settings
    async def get_setting(self, key):
        return await self.pool.fetchval("SELECT value FROM settings WHERE key=$1", key)

    async def set_setting(self, key, value):
        await self.pool.execute(
            "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key, value)

    @staticmethod
    def normalize_channel(value: str) -> str:
        value = value.strip()
        value = re.sub(r"^https?://(?:www\.)?t\.me/", "", value, flags=re.I).rstrip("/").lstrip("@")
        if re.fullmatch(r"-100[1-9]\d*", value):
            return value
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
            raise ValueError("Send a channel @username, t.me/username URL, or -100… channel ID.")
        return value.lower()

    async def monitoring_items(self, kind):
        rows = await self.pool.fetch("SELECT id,value FROM monitoring_items WHERE kind=$1 ORDER BY id", kind)
        return [dict(r) for r in rows]

    async def channels(self) -> list[str]:
        return [r["value"] for r in await self.monitoring_items("channel")]

    async def keywords(self) -> list[str]:
        return [r["value"] for r in await self.monitoring_items("keyword")]

    async def add_monitoring_item(self, actor_id, kind, value):
        if actor_id != OWNER_ID:
            raise PermissionError("Only the bot owner can manage channels and keywords.")
        if kind not in {"channel", "keyword"}:
            raise ValueError("Unknown setting.")
        value = self.normalize_channel(value) if kind == "channel" else value.strip()
        if not value or len(value) > 100:
            raise ValueError("Use 1–100 characters.")
        status = await self.pool.execute("""INSERT INTO monitoring_items(kind,value,normalized)
            SELECT $1::text,$2::text,$3::text
            WHERE $4::bigint=$5::bigint
            ON CONFLICT (kind, normalized) DO NOTHING""",
            kind, value, value.casefold(), actor_id, OWNER_ID)
        return _rowcount(status) > 0

    async def remove_monitoring_item(self, actor_id, kind, item_id):
        if actor_id != OWNER_ID:
            raise PermissionError("Only the bot owner can manage channels and keywords.")
        status = await self.pool.execute("""DELETE FROM monitoring_items WHERE id=$1 AND kind=$2
            AND $3::bigint=$4::bigint""",
            int(item_id), kind, actor_id, OWNER_ID)
        return _rowcount(status) > 0

    # Roles are separate from event/profile data. The owner is configuration-only.
    def require_owner(self, actor_id: int):
        if actor_id != OWNER_ID:
            raise PermissionError("Only the bot owner can manage admins.")

    async def is_admin(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        return await self.pool.fetchval("SELECT 1 FROM admins WHERE user_id=$1", user_id) is not None

    async def list_admins(self) -> list[int]:
        rows = await self.pool.fetch("SELECT user_id FROM admins ORDER BY user_id")
        return [r["user_id"] for r in rows if r["user_id"] != OWNER_ID]

    async def notification_ids(self) -> list[int]:
        return [OWNER_ID, *await self.list_admins()]

    async def add_admin(self, actor_id: int, user_id: int) -> bool:
        self.require_owner(actor_id)
        if user_id <= 0 or user_id == OWNER_ID:
            raise ValueError("Send a positive Telegram user ID other than the owner's ID.")
        status = await self.pool.execute(
            "INSERT INTO admins(user_id, appointed_by, created_at) VALUES($1,$2,$3) ON CONFLICT (user_id) DO NOTHING",
            user_id, actor_id, _now())
        return _rowcount(status) > 0

    async def remove_admin(self, actor_id: int, user_id: int) -> bool:
        self.require_owner(actor_id)
        if user_id == OWNER_ID:
            raise ValueError("The owner cannot be removed through the bot.")
        return _rowcount(await self.pool.execute("DELETE FROM admins WHERE user_id=$1", user_id)) > 0

    @staticmethod
    def _event(row):
        event = dict(row)
        event["title"] = event.get("custom_title") or event.get("title")
        event["stats_stale"] = bool(event.get("last_stats") and
                                    event.get("stats_version") != event.get("settings_version", 0))
        return event

    async def set_event_title(self, actor_id: int, eid: int, title: str):
        title = title.strip()
        if not title or len(title) > 200:
            raise ValueError("The title must contain 1–200 characters.")
        if not await self.is_admin(actor_id):
            raise PermissionError("Admins only.")
        now = _now()
        status = await self.pool.execute("""
            UPDATE events SET custom_title=$1 WHERE id=$2
            AND (start_ts IS NULL OR start_ts <= $3)
            AND (end_ts IS NULL OR end_ts > $3)
            AND ($4::bigint = $5::bigint OR EXISTS (SELECT 1 FROM admins WHERE user_id=$4::bigint))
        """, title, int(eid), now, actor_id, OWNER_ID)
        if _rowcount(status) != 1:
            raise ValueError("Event is missing, no longer live, or your access was revoked.")

    # events
    async def upsert_event(self, ev: dict) -> int:
        e = {"reward_cap": None, "reward_cap_token": None, "start_ts": None, "end_ts": None, "rules": "",
             "min_volume": None, "total_pool": None, "remaining_pool": None, "pool_token": "BNB",
             "leaderboard_url": None, "title": None, **ev}
        f = lambda v: None if v is None else float(v)
        i = lambda v: None if v is None else int(v)
        return await self.pool.fetchval("""
          INSERT INTO events(url,title,start_ts,end_ts,pairs,rules,min_volume,total_pool,remaining_pool,
                             pool_token,fixed_slots,leaderboard_url,reward_cap,reward_cap_token,created_at)
          VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
          ON CONFLICT(url) DO UPDATE SET title=excluded.title,start_ts=excluded.start_ts,end_ts=excluded.end_ts,
            pairs=excluded.pairs,rules=excluded.rules,total_pool=COALESCE(excluded.total_pool, events.total_pool),
            remaining_pool=COALESCE(excluded.remaining_pool, events.remaining_pool),pool_token=excluded.pool_token,
            fixed_slots=CASE WHEN excluded.fixed_slots IN ('[]','') THEN events.fixed_slots ELSE excluded.fixed_slots END,
            min_volume=COALESCE(excluded.min_volume, events.min_volume),
            leaderboard_url=COALESCE(excluded.leaderboard_url, events.leaderboard_url),
            reward_cap=excluded.reward_cap,reward_cap_token=excluded.reward_cap_token,
            settings_version=events.settings_version+1
          RETURNING id
        """, e["url"], e["title"], i(e["start_ts"]), i(e["end_ts"]), json.dumps(e.get("pairs", [])),
            e["rules"], f(e["min_volume"]), f(e["total_pool"]), f(e["remaining_pool"]), e["pool_token"],
            json.dumps(e.get("fixed_slots", [])), e["leaderboard_url"], f(e["reward_cap"]),
            e["reward_cap_token"], _now())

    async def list_events(self, live_only=False):
        q = "SELECT * FROM events"
        args = ()
        if live_only:
            q += " WHERE end_ts IS NULL OR end_ts > $1"
            args = (_now(),)
        q += " ORDER BY end_ts ASC NULLS LAST, id ASC"
        return [self._event(r) for r in await self.pool.fetch(q, *args)]

    async def get_event(self, eid: int):
        r = await self.pool.fetchrow("SELECT * FROM events WHERE id=$1", int(eid))
        return self._event(r) if r else None

    async def update_stats(self, eid: int, stats: dict, expected_version=None):
        status = await self.pool.execute("""UPDATE events SET last_stats=$1, last_scraped=$2,
            stats_version=settings_version WHERE id=$3
            AND ($4::integer IS NULL OR settings_version=$4::integer)""",
            json.dumps(stats), _now(), int(eid), expected_version)
        return _rowcount(status) == 1

    async def set_field(self, eid: int, field: str, value):
        allowed = {"leaderboard_url", "remaining_pool", "total_pool", "min_volume"}
        if field not in allowed:
            raise ValueError("field not allowed")
        if field != "leaderboard_url" and value is not None:
            value = float(value)
        await self.pool.execute(
            f"UPDATE events SET {field}=$1, settings_version=settings_version+1 WHERE id=$2", value, int(eid))

    async def set_fixed_slots(self, eid: int, slots: list):
        await self.pool.execute("UPDATE events SET fixed_slots=$1 WHERE id=$2", json.dumps(slots), int(eid))

    async def delete_event(self, eid: int):
        await self.pool.execute("DELETE FROM events WHERE id=$1", int(eid))

    async def purge_old(self, days: int) -> int:
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        status = await self.pool.execute("DELETE FROM events WHERE end_ts IS NOT NULL AND end_ts < $1", cutoff)
        return _rowcount(status)

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None


db = DB()
