import json
from datetime import datetime, timezone
from html import escape
from config import FALLBACK_REWARD_CAP, FALLBACK_REWARD_TOKEN
from reward_caps import effective_reward_cap
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M


def is_live(ev) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    return (ev.get("start_ts") is None or ev["start_ts"] <= now) and (
        ev.get("end_ts") is None or ev["end_ts"] > now)


def status(ev) -> str:
    now = datetime.now(timezone.utc).timestamp()
    end = ev.get("end_ts")
    start = ev.get("start_ts")
    if start and now < start:
        return f"🟡 Upcoming (starts in {_dur(start - now)})"
    if end is None:
        return "🟢 Live (end date unknown)"
    if now < end:
        return f"🟢 Live — {_dur(end - now)} left"
    return "🔴 Closed"


def _dur(sec: float) -> str:
    sec = int(sec)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m"


def _ts(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "?"


def _volume_line(stats) -> str:
    if not stats:
        return "📊 <b>Total Volume (Rank 1001+): pending first scrape</b>"
    return f"📊 <b>Total Volume (Rank 1001+): ${stats['total_volume']:,.0f}</b>"


def event_text(ev) -> str:
    pairs = json.loads(ev.get("pairs") or "[]")
    stats = json.loads(ev["last_stats"]) if ev.get("last_stats") else None
    tok = ev.get("pool_token") or "BNB"
    lines = [
        f"<b>{escape(ev.get('title') or 'Event')}</b>",
        status(ev),
        _volume_line(stats),
        "👥 <b>Total Participants: " + (f"{stats['total_participants']:,}" if stats and stats.get("total_participants") is not None else "unavailable from source") + "</b>",
        "👤 <b>Displayed Participants (Ranked): " + (f"{stats['ranked_participants']:,}" if stats and stats.get("ranked_participants") is not None else "pending full scrape") + "</b>",
        f"📅 {_ts(ev.get('start_ts'))} → {_ts(ev.get('end_ts'))}",
        f"💰 Prize pool: {ev.get('total_pool') or '?'} {tok}",
        f"🎯 Remaining pool (rank 1001+): {ev.get('remaining_pool') or 'not set'} {tok}",
        f"💱 Pairs: {escape(', '.join(pairs)) or '?'}",
    ]
    cap, source = effective_reward_cap(ev, FALLBACK_REWARD_CAP, FALLBACK_REWARD_TOKEN)
    lines.append(f"🧢 Per-user cap: {cap:g} {tok} ({source})" if cap is not None else "🧢 Per-user cap: unavailable in this reward token")
    if ev.get("min_volume"):
        lines.append(f"📏 Min volume: ${ev['min_volume']:,.0f}")
    slots = json.loads(ev.get("fixed_slots") or "[]")
    if slots:
        lines.append("\n<b>Fixed rewards</b>")
        for s in slots:
            per = s["amount"] / (s["to"] - s["from"] + 1) if s.get("split") else s["amount"]
            rng = f"#{s['from']}" if s["from"] == s["to"] else f"#{s['from']}–{s['to']}"
            lines.append(f"{rng}: {per:.4f} {tok} each")
    if stats:
        lines += ["\n<b>Estimate (rank 1001+)</b>",
                  f"👥 Participants (Rank 1001+): {stats['participants']:,}"]
        if not ev.get("stats_stale") and stats.get("has_estimate", stats.get("per_dollar", 0) > 0):
            lines += [f"🏷 Per $1,000 volume: <b>{stats['per_1000']:.6f} {tok}</b>",
                      f"🧢 Cap {stats['cap']} {tok} reached at ${stats['volume_to_hit_cap']:,.0f}"]
        else:
            lines.append("⚠️ Settings changed; previous complete counts retained. Estimate pending refresh."
                         if ev.get("stats_stale") else "ℹ️ Set the remaining pool to see reward per $1,000.")
        lines.append(f"🕒 Updated {_ts(ev.get('last_scraped'))}")
    else:
        lines.append("\n⏳ Leaderboard estimate not available yet.")
    lines.append(f"\n🔗 <a href=\"{escape(ev['url'])}\">Announcement</a>")
    return "\n".join(lines)


def events_kb(events, prefix="ev") -> M:
    rows = [[B(f"{'🟢' if 'Live' in status(e) else '🔴'} {(e.get('title') or 'Event')[:50]}", callback_data=f"{prefix}:{e['id']}")]
            for e in events]
    rows.append([B("🔄 Refresh", callback_data="home")])
    return M(rows)


def event_kb(eid, admin=False) -> M:
    rows = [[B("🔄 Refresh", callback_data=f"ev:{eid}"), B("⬅️ Back", callback_data="home")]]
    if admin:
        rows.insert(0, [B("⚙️ Edit event", callback_data=f"aedit:{eid}"),
                        B("🗑 Delete", callback_data=f"adel:{eid}")])
    return M(rows)


def admin_kb(owner=False) -> M:
    rows = [
        [B("📡 Channels", callback_data="a:chan"), B("🔑 Keywords", callback_data="a:kw")],
        [B("➕ Add event by link", callback_data="a:add")],
        [B("📋 Manage events", callback_data="a:list")],
        [B("▶️ Run scrape now", callback_data="a:run")],
    ]
    if owner:
        rows.append([B("👥 Manage admins", callback_data="o:admins")])
    return M(rows)


def admins_kb(admin_ids) -> M:
    rows = [[B(f"❌ Revoke {aid}", callback_data=f"o:remove:{aid}")] for aid in admin_ids]
    rows.append([B("➕ Appoint admin", callback_data="o:add")])
    rows.append([B("⬅️ Back", callback_data="a:home")])
    return M(rows)


def settings_kb(items, kind) -> M:
    prefix = "chdel" if kind == "channel" else "kwdel"
    add = "a:chadd" if kind == "channel" else "a:kwadd"
    label = "channel" if kind == "channel" else "keyword"
    rows = [[B(f"❌ {r['value'][:45]}", callback_data=f"{prefix}:{r['id']}")] for r in items]
    rows.append([B(f"➕ Add {label}", callback_data=add), B("⬅️ Back", callback_data="a:home")])
    return M(rows)


def edit_kb(eid, live=False) -> M:
    rows = [
        [B("Remaining pool", callback_data=f"ef:{eid}:remaining_pool"),
         B("Total pool", callback_data=f"ef:{eid}:total_pool")],
        [B("Leaderboard URL", callback_data=f"ef:{eid}:leaderboard_url"),
         B("Fixed slots", callback_data=f"ef:{eid}:fixed_slots")],
        [B("⬅️ Back", callback_data=f"ev:{eid}")],
    ]
    if live:
        rows.insert(0, [B("✏️ Edit title", callback_data=f"ef:{eid}:title")])
    return M(rows)
