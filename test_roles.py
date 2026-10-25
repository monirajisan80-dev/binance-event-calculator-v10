"""Role-based menu visibility and help-manual tests (no DB/network)."""
import os
os.environ.setdefault("BOT_TOKEN", "123456:dummy-test-token")
os.environ.setdefault("OWNER_ID", "111")
import ui


def cbs(kb):
    return {b.callback_data for row in kb.inline_keyboard for b in row}


def test_regular_user_sees_no_admin_or_owner_buttons():
    data = cbs(ui.events_kb([], role=ui.ROLE_USER)) | cbs(ui.help_kb(ui.ROLE_USER))
    assert not any(d.startswith(("a:", "o:")) for d in data)
    assert "help" in data
    assert cbs(ui.event_kb(1, admin=False)) == {"ev:1", "home"}


def test_admin_sees_admin_tools_but_not_owner_controls():
    data = cbs(ui.admin_kb(owner=False))
    assert {"a:add", "a:list", "a:run"} <= data
    assert not any(d.startswith("o:") for d in data)
    assert "a:home" in cbs(ui.events_kb([], role=ui.ROLE_ADMIN))


def test_owner_sees_all_master_controls():
    data = cbs(ui.admin_kb(owner=True))
    assert {"a:add", "a:list", "a:run", "o:chan", "o:kw", "o:admins"} <= data


def test_help_text_is_tailored_per_role():
    u, a, o = (ui.help_text(r) for r in (ui.ROLE_USER, ui.ROLE_ADMIN, ui.ROLE_OWNER))
    assert "Admin guide" not in u and "Owner controls" not in u
    assert "Admin guide" in a and "Owner controls" not in a
    assert "Admin guide" in o and "Owner controls" in o
