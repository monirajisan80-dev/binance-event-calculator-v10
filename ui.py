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


ROLE_USER, ROLE_ADMIN, ROLE_OWNER = "user", "admin", "owner"


def events_kb(events, prefix="ev", role=ROLE_USER) -> M:
    rows = [[B(f"{'🟢' if 'Live' in status(e) else '🔴'} {(e.get('title') or 'Event')[:50]}", callback_data=f"{prefix}:{e['id']}")]
            for e in events]
    rows.append([B("🔄 Refresh", callback_data="home"), B("📖 Help", callback_data="help")])
    if role in (ROLE_ADMIN, ROLE_OWNER):
        rows.append([B("🛠 Admin panel", callback_data="a:home")])
    return M(rows)


def help_kb(role=ROLE_USER) -> M:
    rows = [[B("⬅️ Back to events", callback_data="home")]]
    if role in (ROLE_ADMIN, ROLE_OWNER):
        rows.append([B("🛠 Admin panel", callback_data="a:home")])
    return M(rows)


def help_text(role=ROLE_USER) -> str:
    parts = [
        "<b>📖 User manual</b>",
        "This bot tracks Binance trading-competition events and estimates what you could earn "
        "if you finish outside the top 1,000.",
        "\n<b>Commands</b>\n/start — list live events\n/help — this manual\n/cancel — cancel what you are typing",
        "\n<b>Reading an event</b>\n"
        "• <b>Status</b> — upcoming, live (time left) or closed\n"
        "• <b>Total Volume (Rank 1001+)</b> — combined trading volume of everyone ranked 1001 and below\n"
        "• <b>Total / Displayed Participants</b> — all entrants vs. those shown on the leaderboard\n"
        "• <b>Prize pool / Remaining pool</b> — full prize vs. the part shared by rank 1001+\n"
        "• <b>Per $1,000 volume</b> — estimated reward for each $1,000 you trade\n"
        "• <b>Per-user cap</b> — the most one person can win; the bot shows the volume where you hit it\n"
        "• <b>Fixed rewards</b> — set prizes for specific ranks\n"
        "Estimates refresh automatically. Tap 🔄 Refresh to reload.",
    ]
    if role in (ROLE_ADMIN, ROLE_OWNER):
        parts.append(
            "\n<b>🛠 Admin guide</b> (/admin)\n"
            "• <b>Add event by link</b> — paste a Binance announcement link; details are read automatically\n"
            "• <b>Manage events</b> — open any event, then ⚙️ Edit to set the remaining pool, total pool, "
            "leaderboard link, fixed reward slots, or title (live events only)\n"
            "• Setting the <b>remaining pool</b> and <b>leaderboard link</b> is what unlocks the per-$1,000 estimate\n"
            "• Fixed slots format, one per line: <code>1-1 10</code>, <code>6-20 15 split</code>\n"
            "• <b>Run scrape now</b> — refresh every event immediately\n"
            "• 🗑 Delete removes an event")
    if role == ROLE_OWNER:
        parts.append(
            "\n<b>👑 Owner controls</b>\n"
            "• <b>Channels</b> — Telegram channels watched for new events (@name, t.me link or -100… ID)\n"
            "• <b>Keywords</b> — words that mark a channel post as a new event\n"
            "• <b>Manage admins</b> — appoint admins by numeric Telegram ID, or revoke them")
    return "\n".join(parts)


def event_kb(eid, admin=False) -> M:
    rows = [[B("🔄 Refresh", callback_data=f"ev:{eid}"), B("⬅️ Back", callback_data="home")]]
    if admin:
        rows.insert(0, [B("⚙️ Edit event", callback_data=f"aedit:{eid}"),
                        B("🗑 Delete", callback_data=f"adel:{eid}")])
    return M(rows)


def admin_kb(owner=False) -> M:
    rows = [
        [B("➕ Add event by link", callback_data="a:add")],
        [B("📋 Manage events", callback_data="a:list")],
        [B("▶️ Run scrape now", callback_data="a:run")],
    ]
    if owner:
        rows.append([B("📡 Channels", callback_data="o:chan"), B("🔑 Keywords", callback_data="o:kw")])
        rows.append([B("👥 Manage admins", callback_data="o:admins")])
    rows.append([B("📖 Help", callback_data="help"), B("⬅️ Events", callback_data="home")])
    return M(rows)


def admins_kb(admin_ids) -> M:
    rows = [[B(f"❌ Revoke {aid}", callback_data=f"o:remove:{aid}")] for aid in admin_ids]
    rows.append([B("➕ Appoint admin", callback_data="o:add")])
    rows.append([B("⬅️ Back", callback_data="a:home")])
    return M(rows)


def settings_kb(items, kind) -> M:
    prefix = "o:chdel" if kind == "channel" else "o:kwdel"
    add = "o:chadd" if kind == "channel" else "o:kwadd"
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
