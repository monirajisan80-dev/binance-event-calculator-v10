"""Monitor robustness regression tests, no network required."""
import asyncio
import os

os.environ.setdefault("BOT_TOKEN", "123456:dummy-test-token")
os.environ.setdefault("OWNER_ID", "111")

from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import UserAlreadyParticipantError

import monitor as mon

_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


def test_start_with_bad_session_string_disables_monitoring_without_raising():
    """A malformed TG_SESSION_STRING must not crash the whole bot."""
    m = mon.Monitor(AsyncMock())
    with patch.object(mon, "API_ID", 123), patch.object(mon, "API_HASH", "hash"), \
            patch.object(mon, "SESSION_STRING", "not-a-real-session"), \
            patch.object(mon, "TelegramClient", side_effect=ValueError("bad session")):
        run(m.start())  # must not raise
    assert m.client is None
    m.notify.assert_awaited_once()


def _raise_already_participant(*a, **k):
    raise UserAlreadyParticipantError(request=None)


def test_already_joined_channel_counts_as_subscribed():
    """UserAlreadyParticipantError means success: no false 'cannot monitor' warning."""
    m = mon.Monitor(AsyncMock())
    m.client = MagicMock()
    m.client.get_entity = AsyncMock(return_value="entity")
    m.client.side_effect = _raise_already_participant

    class FakeDB:
        async def channels(self):
            return ["somechannel"]

    with patch.object(mon, "db", FakeDB()):
        run(m.sync_channels())

    assert "somechannel" in m._active
    m.notify.assert_not_awaited()


def test_genuine_subscription_failure_notifies_once():
    m = mon.Monitor(AsyncMock())
    m.client = MagicMock()
    m.client.get_entity = AsyncMock(side_effect=ValueError("no access"))

    class FakeDB:
        async def channels(self):
            return ["somechannel"]

    with patch.object(mon, "db", FakeDB()):
        run(m.sync_channels())
        run(m.sync_channels())  # retry must not spam the owner again

    assert "somechannel" not in m._active
    m.notify.assert_awaited_once()
