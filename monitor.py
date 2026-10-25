"""
Channel monitoring via Telethon (a user account), because a Bot API bot cannot read
public channels it is not an admin of. Channel and keywords are re-read from the DB on
every message. New accessible public channels are subscribed without a restart.
"""
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.errors import UserAlreadyParticipantError
from config import API_ID, API_HASH, SESSION_STRING
from db import db
from jobs import ingest_link
from scraper import extract_links, is_binance_url

log = logging.getLogger("monitor")


class Monitor:
    def __init__(self, notify):
        self.client: TelegramClient | None = None
        self.notify = notify
        self._sync_task = None
        self._active = set()
        self._reported_errors = set()

    async def sync_channels(self):
        if not self.client:
            return
        targets = set(await db.channels())
        self._active.intersection_update(targets)
        self._reported_errors.intersection_update(targets)
        for target in targets - self._active:
            try:
                entity = await self.client.get_entity(int(target) if target.startswith("-100") else target)
                try:
                    await self.client(JoinChannelRequest(entity))
                except UserAlreadyParticipantError:
                    pass  # already following the channel — that is the goal, not an error
                self._active.add(target)
                self._reported_errors.discard(target)
            except Exception:
                log.exception("Cannot subscribe to channel %s", target)
                if target not in self._reported_errors:
                    await self.notify(f"⚠️ Cannot monitor {target} yet. Check the monitoring account's channel access; retrying automatically.")
                    self._reported_errors.add(target)

    async def _sync_loop(self):
        while True:
            try:
                await self.sync_channels()
            except Exception:
                log.exception("Channel subscription refresh failed")
            await asyncio.sleep(15)

    async def handle_message(self, event):
        try:
            if not event.is_channel:
                return
            chat = await event.get_chat()
            aliases = {(getattr(chat, "username", "") or "").lower(), str(event.chat_id)}
            if not aliases.intersection(await db.channels()):
                return
            text = event.raw_text or ""
            hit = next((k for k in await db.keywords() if k.casefold() in text.casefold()), None)
            if not hit:
                return
            links = extract_links(text)
            for ent_url in _entity_urls(event.message):
                if is_binance_url(ent_url) and ent_url not in links:
                    links.append(ent_url)
            for url in links:
                eid = await ingest_link(url)
                await self.notify(f"🆕 Matched “{hit}” → event #{eid}\n{url}")
                await asyncio.sleep(1)
        except Exception:
            log.exception("monitor handler error")

    async def start(self):
        if not (API_ID and API_HASH and SESSION_STRING):
            log.warning("Telethon credentials missing; channel monitoring disabled")
            return
        try:
            self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
            await self.client.start()
        except Exception:
            # A bad/expired session string must not take down the whole bot.
            log.exception("Telethon login failed; channel monitoring disabled")
            self.client = None
            try:
                await self.notify("⚠️ Channel monitoring is off: the monitoring account login failed "
                                  "(check TG_SESSION_STRING / TG_API_ID / TG_API_HASH). The rest of the bot is running.")
            except Exception:
                log.warning("Could not notify admins about the Telethon login failure")
            return
        self.client.add_event_handler(self.handle_message, events.NewMessage())
        self._sync_task = asyncio.create_task(self._sync_loop())
        log.info("Channel monitor started")

    async def stop(self):
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.disconnect()


def _entity_urls(msg):
    out = []
    for ent in msg.entities or []:
        url = getattr(ent, "url", None)
        if url:
            out.append(url)
    if msg.reply_markup and hasattr(msg.reply_markup, "rows"):
        for row in msg.reply_markup.rows:
            for b in row.buttons:
                if getattr(b, "url", None):
                    out.append(b.url)
    return out
