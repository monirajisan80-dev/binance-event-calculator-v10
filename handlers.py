import json
import logging
import math
import re
from html import escape
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from config import OWNER_ID
from db import db
from jobs import ingest_link, scrape_cycle, refresh_event
import ui
from scraper import is_binance_url

log = logging.getLogger("handlers")


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


async def is_admin(update: Update) -> bool:
    return bool(update.effective_user and await db.is_admin(update.effective_user.id))


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("await", None)
    await update.effective_chat.send_message("Cancelled.")


async def manage_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await _edit_or_send(update, "⛔ Only the bot owner can manage admins.", None)
    ids = await db.list_admins()
    names = "\n".join(f"• <code>{aid}</code>" for aid in ids) or "No appointed admins."
    await _edit_or_send(update, f"<b>Manage admins</b>\nOwner: <code>{OWNER_ID}</code>\n\n{names}",
                        ui.admins_kb(ids))


async def _edit_or_send(update: Update, text, kb):
    q = update.callback_query
    if q:
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
            return
        except Exception:
            pass  # "message not modified" etc.
    await update.effective_chat.send_message(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                             disable_web_page_preview=True)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("await", None)
    events = await db.list_events(live_only=True)
    text = "<b>Live Binance events</b>\nTap one for details." if events else "No live events right now."
    await _edit_or_send(update, text, ui.events_kb(events))


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("await", None)
    if not await is_admin(update):
        return await update.effective_chat.send_message("⛔ Admins only.")
    channels = await db.channels()
    kws = await db.keywords()
    role = "Owner" if is_owner(update) else "Admin"
    await _edit_or_send(update, f"<b>Admin panel — {role}</b>\nChannels: {escape(', '.join(channels)) or 'none'}\nKeywords: {escape(', '.join(kws))}", ui.admin_kb(is_owner(update)))


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return
    try:
        await q.answer()
    except Exception:
        pass  # stale/expired callback query; answering is best-effort
    ctx.user_data.pop("await", None)
    data = q.data or ""
    try:
        if data == "home":
            return await start(update, ctx)
        if data.startswith("ev:"):
            ev = await db.get_event(int(data[3:]))
            if not ev:
                return await _edit_or_send(update, "Event not found.", ui.events_kb([]))
            return await _edit_or_send(update, ui.event_text(ev), ui.event_kb(ev["id"], await is_admin(update)))

        # Owner permission is checked independently of visible buttons.
        if data.startswith("o:"):
            if not is_owner(update):
                return await q.message.reply_text("⛔ Only the bot owner can manage admins.")
            if data == "o:admins":
                return await manage_admins(update, ctx)
            if data == "o:add":
                ctx.user_data["await"] = ("admin_add",)
                return await q.message.reply_text("Send the new admin's numeric Telegram user ID. /cancel to cancel.")
            if data.startswith("o:remove:"):
                aid = int(data.rsplit(":", 1)[1])
                if aid == OWNER_ID:
                    return await q.message.reply_text("The owner cannot be removed through the bot.")
                kb = ui.M([[ui.B("✅ Revoke access", callback_data=f"o:confirm:{aid}"),
                            ui.B("Cancel", callback_data="o:admins")]])
                return await _edit_or_send(update, f"Revoke admin access for <code>{aid}</code>?", kb)
            if data.startswith("o:confirm:"):
                removed = await db.remove_admin(update.effective_user.id, int(data.rsplit(":", 1)[1]))
                await q.message.reply_text("✅ Admin access revoked." if removed else "This user is not an admin.")
                return await manage_admins(update, ctx)
            return

        # ---- admin only below ----
        if not await is_admin(update):
            return await q.message.reply_text("⛔ Admins only. Your access may have been revoked.")
        if data == "a:home":
            return await admin(update, ctx)
        if data in {"a:chan", "a:kw"}:
            kind = "channel" if data == "a:chan" else "keyword"
            return await _edit_or_send(update, f"{kind.title()}s (tap to remove):",
                                       ui.settings_kb(await db.monitoring_items(kind), kind))
        if data in {"a:chadd", "a:kwadd"}:
            kind = "channel" if data == "a:chadd" else "keyword"
            ctx.user_data["await"] = ("setting_add", kind)
            hint = "Send a channel @username, t.me/username URL, or -100… channel ID." if kind == "channel" else "Send the new keyword (1–100 characters)."
            return await q.message.reply_text(hint + " /cancel to cancel.")
        if data.startswith(("chdel:", "kwdel:")):
            prefix, item_id = data.split(":")
            kind = "channel" if prefix == "chdel" else "keyword"
            await db.remove_monitoring_item(update.effective_user.id, kind, int(item_id))
            return await _edit_or_send(update, f"{kind.title()}s (tap to remove):",
                                       ui.settings_kb(await db.monitoring_items(kind), kind))
        if data == "a:add":
            ctx.user_data["await"] = ("add",)
            return await q.message.reply_text("Send a Binance announcement link.")
        if data == "a:list":
            evs = await db.list_events()
            return await _edit_or_send(update, "All events:" if evs else "No events.", ui.events_kb(evs))
        if data == "a:run":
            await q.message.reply_text("⏳ Running scrape…")
            res = await scrape_cycle()
            return await q.message.reply_text(f"✅ Done: {res}")
        if data.startswith("adel:"):
            await db.delete_event(int(data[5:]))
            await q.message.reply_text("🗑 Deleted.")
            return await start(update, ctx)
        if data.startswith("aedit:"):
            ev = await db.get_event(int(data[6:]))
            if not ev:
                return await q.message.reply_text("Event not found.")
            return await _edit_or_send(update, "What do you want to set?", ui.edit_kb(ev["id"], ui.is_live(ev)))
        if data.startswith("ef:"):
            _, eid, field = data.split(":")
            if field not in {"title", "remaining_pool", "total_pool", "leaderboard_url", "fixed_slots", "min_volume"}:
                return await q.message.reply_text("Unknown event field.")
            ev = await db.get_event(int(eid))
            if not ev:
                return await q.message.reply_text("Event not found.")
            if field == "title" and not ui.is_live(ev):
                return await q.message.reply_text("Titles can only be edited for active/live events.")
            ctx.user_data["await"] = ("field", int(eid), field)
            hint = {
                "title": "Send the new event title (1–200 characters). /cancel to cancel.",
                "fixed_slots": "Send lines like:\n1-1 10\n2-5 3\n6-20 15 split\n(“split” = bucket shared equally)",
                "leaderboard_url": "Send the leaderboard page URL.",
            }.get(field, "Send a number.")
            return await q.message.reply_text(hint)
    except Exception as e:
        log.exception("callback error")
        await q.message.reply_text(f"⚠️ Error: {e}")


def parse_amount(text: str) -> float:
    try:
        v = float(text.replace(",", "").replace("$", "").strip())
    except ValueError:
        raise ValueError("Send a number, e.g. 40 or 12,500.") from None
    if not math.isfinite(v) or v < 0:
        raise ValueError("The number must be zero or positive.")
    return v


def _parse_slots(text: str) -> list[dict]:
    slots = []
    try:
        for line in text.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            a, b = parts[0].split("-", 1) if "-" in parts[0] else (parts[0], parts[0])
            a, b = int(a), int(b)
            if a < 1 or b < a:
                raise ValueError
            slots.append({"from": a, "to": b, "amount": parse_amount(parts[1]),
                          "split": len(parts) > 2 and parts[2].lower() == "split"})
    except (ValueError, IndexError):
        raise ValueError("Use lines like “1-1 10” or “6-20 15 split”.") from None
    if not slots:
        raise ValueError("Send at least one slot line.")
    return slots


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pending = ctx.user_data.pop("await", None)
    if not pending:
        return
    if pending[0] == "admin_add" and not is_owner(update):
        return await update.message.reply_text("⛔ Only the bot owner can manage admins.")
    if not await is_admin(update):
        return await update.message.reply_text("⛔ Admins only. Your access may have been revoked.")
    text = (update.message.text or "").strip()
    try:
        kind = pending[0]
        if kind == "admin_add":
            if not text.isascii() or not text.isdecimal() or int(text) <= 0:
                raise ValueError("Send a positive numeric Telegram user ID, not a username.")
            added = await db.add_admin(update.effective_user.id, int(text))
            await update.message.reply_text("✅ Admin appointed. They can open /admin." if added else "This user is already an admin.")
            return await manage_admins(update, ctx)
        if kind == "setting_add":
            setting_kind = pending[1]
            added = await db.add_monitoring_item(update.effective_user.id, setting_kind, text)
            await update.message.reply_text("✅ Saved." if added else "Already in the list.")
            return await _edit_or_send(update, f"{setting_kind.title()}s (tap to remove):",
                                       ui.settings_kb(await db.monitoring_items(setting_kind), setting_kind))
        if kind == "add":
            if not is_binance_url(text):
                return await update.message.reply_text("That isn't a Binance link.")
            await update.message.reply_text("⏳ Scraping…")
            eid = await ingest_link(text)
            ev = await db.get_event(eid)
            return await update.message.reply_text(ui.event_text(ev), reply_markup=ui.event_kb(eid, True),
                                                   parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        if kind == "field":
            _, eid, field = pending
            if not await db.get_event(eid):
                return await update.message.reply_text("Event not found.")
            if field == "title":
                await db.set_event_title(update.effective_user.id, eid, text)
                await update.message.reply_text("✅ Event title saved.")
            elif field == "fixed_slots":
                slots = _parse_slots(text)
                await db.set_fixed_slots(eid, slots)
            elif field == "leaderboard_url":
                if not is_binance_url(text):
                    raise ValueError("Send a https://www.binance.com/… leaderboard link.")
                await db.set_field(eid, field, text)
            else:
                await db.set_field(eid, field, parse_amount(text))
            ev = await db.get_event(eid)
            if field in ("remaining_pool", "leaderboard_url"):
                await update.message.reply_text("✅ Saved. Refreshing estimate…")
                try:
                    await refresh_event(ev)
                    ev = await db.get_event(eid)
                except Exception as e:
                    await update.message.reply_text(f"⚠️ Leaderboard scrape failed: {e}")
            return await update.message.reply_text(ui.event_text(ev), reply_markup=ui.event_kb(eid, True),
                                                   parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        log.exception("text handler error")
        if isinstance(e, ValueError):
            ctx.user_data["await"] = pending
        await update.message.reply_text(f"⚠️ Error: {e}\n/cancel to cancel.")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=ctx.error)
