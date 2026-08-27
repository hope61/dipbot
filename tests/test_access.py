"""Access control: owner, admins, and everyone else.

The dangerous failures here are all silent ones - a stranger getting in, a
removed admin still working, or an admin locking the owner out. Each has a test.
"""
from __future__ import annotations

import pytest

from dipbot.bot.middleware import ADMIN, OWNER, AccessMiddleware

pytestmark = pytest.mark.asyncio

OWNER_ID = 1919116430
ADMIN_ID = 555000111
STRANGER_ID = 424242


class FakeUser:
    def __init__(self, user_id: int, username: str = "someone"):
        self.id = user_id
        self.username = username


class FakeMessage:
    """Stands in for aiogram's Message - only from_user and answer are used."""

    def __init__(self, user_id: int):
        self.from_user = FakeUser(user_id)
        self.replies: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeCallback:
    #: Presence of `data` is how the gate tells a callback from a message.
    data = "noop"

    def __init__(self, user_id: int):
        self.from_user = FakeUser(user_id)
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kwargs) -> None:
        self.answers.append(text)


class NoUserEvent:
    """A channel post or service update: no meaningful from_user."""

    from_user = None


def make_handler():
    seen: list[dict] = []

    async def handler(event, data):
        seen.append(dict(data))
        return "ran"

    return handler, seen


# --- roles ------------------------------------------------------------------


async def test_owner_is_recognised(db):
    gate = AccessMiddleware(OWNER_ID, db)
    assert await gate.role_of(OWNER_ID) == OWNER


async def test_admin_is_recognised(db):
    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    gate = AccessMiddleware(OWNER_ID, db)
    assert await gate.role_of(ADMIN_ID) == ADMIN


async def test_stranger_has_no_role(db):
    gate = AccessMiddleware(OWNER_ID, db)
    assert await gate.role_of(STRANGER_ID) is None


async def test_owner_stays_owner_even_if_also_in_admins(db):
    """A stray row must never demote the owner."""
    await db.add_admin(OWNER_ID, "oops", OWNER_ID)
    gate = AccessMiddleware(OWNER_ID, db)
    assert await gate.role_of(OWNER_ID) == OWNER


# --- gating -----------------------------------------------------------------


async def test_owner_passes_through(db):
    gate = AccessMiddleware(OWNER_ID, db)
    handler, seen = make_handler()
    assert await gate(handler, FakeMessage(OWNER_ID), {}) == "ran"
    assert seen[0]["role"] == OWNER
    assert seen[0]["is_owner"] is True


async def test_admin_passes_through(db):
    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    gate = AccessMiddleware(OWNER_ID, db)
    handler, seen = make_handler()
    assert await gate(handler, FakeMessage(ADMIN_ID), {}) == "ran"
    assert seen[0]["role"] == ADMIN
    assert seen[0]["is_owner"] is False


async def test_stranger_is_blocked_and_told_once(db):
    gate = AccessMiddleware(OWNER_ID, db)
    handler, seen = make_handler()
    message = FakeMessage(STRANGER_ID)

    assert await gate(handler, message, {}) is None
    assert seen == []
    assert message.replies == ["This bot is private."]
    assert gate.rejected == 1


async def test_stranger_callback_is_blocked(db):
    gate = AccessMiddleware(OWNER_ID, db)
    handler, seen = make_handler()
    query = FakeCallback(STRANGER_ID)

    assert await gate(handler, query, {}) is None
    assert seen == []
    assert query.answers == ["Not allowed."]


async def test_events_without_a_user_are_dropped(db):
    """Channel posts have no from_user; they must not reach handlers."""
    gate = AccessMiddleware(OWNER_ID, db)
    handler, seen = make_handler()
    assert await gate(handler, NoUserEvent(), {}) is None
    assert seen == []


# --- revocation takes effect immediately ------------------------------------


async def test_removed_admin_loses_access_immediately(db):
    """Permissions are read per update, so there is no stale-cache window."""
    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    gate = AccessMiddleware(OWNER_ID, db)
    handler, _ = make_handler()

    assert await gate(handler, FakeMessage(ADMIN_ID), {}) == "ran"

    await db.remove_admin(ADMIN_ID)
    message = FakeMessage(ADMIN_ID)
    assert await gate(handler, message, {}) is None
    assert message.replies == ["This bot is private."]


async def test_newly_added_admin_works_without_restart(db):
    gate = AccessMiddleware(OWNER_ID, db)
    handler, _ = make_handler()

    assert await gate(handler, FakeMessage(ADMIN_ID), {}) is None

    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    assert await gate(handler, FakeMessage(ADMIN_ID), {}) == "ran"


# --- the owner cannot be locked out -----------------------------------------


async def test_owner_is_never_stored_as_an_admin_row(db):
    """The owner comes from config, so no database edit can revoke them."""
    gate = AccessMiddleware(OWNER_ID, db)
    assert OWNER_ID not in await db.admin_ids()
    assert await gate.role_of(OWNER_ID) == OWNER


async def test_clearing_every_admin_leaves_the_owner(db):
    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    await db.add_admin(999, "Carol", OWNER_ID)
    for admin in list(await db.admin_ids()):
        await db.remove_admin(admin)

    gate = AccessMiddleware(OWNER_ID, db)
    assert await gate.role_of(OWNER_ID) == OWNER
    assert await db.admin_ids() == set()


# --- storage ----------------------------------------------------------------


async def test_admin_records_who_added_them(db):
    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    entry = (await db.list_admins())[0]
    assert entry["user_id"] == ADMIN_ID
    assert entry["label"] == "Bob"
    assert entry["added_by"] == OWNER_ID
    assert entry["added_at"] > 0


async def test_adding_twice_is_reported(db):
    assert await db.add_admin(ADMIN_ID, "Bob", OWNER_ID) is True
    assert await db.add_admin(ADMIN_ID, "Bob again", OWNER_ID) is False
    assert len(await db.list_admins()) == 1


async def test_removing_a_non_admin_is_reported(db):
    assert await db.remove_admin(STRANGER_ID) is False


async def test_admins_persist_across_reconnect(db):
    from dipbot.db import Database

    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    await db.close()

    reopened = Database(db.path)
    await reopened.connect()
    assert await reopened.admin_ids() == {ADMIN_ID}
    await reopened.close()


async def test_label_is_optional(db):
    await db.add_admin(ADMIN_ID, None, OWNER_ID)
    assert (await db.list_admins())[0]["label"] is None


async def test_many_admins_all_have_access(db):
    ids = {111, 222, 333, 444}
    for user_id in ids:
        await db.add_admin(user_id, None, OWNER_ID)

    gate = AccessMiddleware(OWNER_ID, db)
    for user_id in ids:
        assert await gate.role_of(user_id) == ADMIN
    assert await db.admin_ids() == ids


# --- owner-only commands ----------------------------------------------------
# An admin managing admins would let them add an accomplice or remove peers.


class Recorder(FakeMessage):
    def __init__(self, user_id: int, text: str):
        super().__init__(user_id)
        self.text = text


async def _run(command, user_id, text, db, is_owner):
    from dipbot.bot import handlers

    handlers.deps = handlers.Deps(
        db=db, dex=None, sender=None, owner_id=OWNER_ID,
    )
    message = Recorder(user_id, text)
    await command(message, is_owner=is_owner)
    return message.replies


async def test_admin_cannot_add_another_admin(db):
    from dipbot.bot.handlers import cmd_addadmin

    replies = await _run(cmd_addadmin, ADMIN_ID, f"/addadmin {STRANGER_ID}", db, is_owner=False)
    assert "Only the owner" in replies[0]
    assert await db.admin_ids() == set()


async def test_admin_cannot_remove_an_admin(db):
    from dipbot.bot.handlers import cmd_removeadmin

    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    replies = await _run(cmd_removeadmin, ADMIN_ID, f"/removeadmin {ADMIN_ID}", db, is_owner=False)
    assert "Only the owner" in replies[0]
    assert await db.admin_ids() == {ADMIN_ID}


async def test_owner_can_add_an_admin(db):
    from dipbot.bot.handlers import cmd_addadmin

    replies = await _run(cmd_addadmin, OWNER_ID, f"/addadmin {ADMIN_ID} Bob", db, is_owner=True)
    assert "Added" in replies[0]
    assert await db.admin_ids() == {ADMIN_ID}
    assert (await db.list_admins())[0]["label"] == "Bob"


async def test_owner_cannot_remove_themselves(db):
    from dipbot.bot.handlers import cmd_removeadmin

    replies = await _run(cmd_removeadmin, OWNER_ID, f"/removeadmin {OWNER_ID}", db, is_owner=True)
    assert "can't be removed" in replies[0]


async def test_adding_the_owner_is_a_no_op(db):
    from dipbot.bot.handlers import cmd_addadmin

    replies = await _run(cmd_addadmin, OWNER_ID, f"/addadmin {OWNER_ID}", db, is_owner=True)
    assert "already has full access" in replies[0]
    assert await db.admin_ids() == set()


async def test_non_numeric_id_is_rejected_with_guidance(db):
    from dipbot.bot.handlers import cmd_addadmin

    replies = await _run(cmd_addadmin, OWNER_ID, "/addadmin @someone", db, is_owner=True)
    assert "numeric user id" in replies[0]
    assert "@userinfobot" in replies[0]
    assert await db.admin_ids() == set()


async def test_addadmin_without_an_argument_explains_itself(db):
    from dipbot.bot.handlers import cmd_addadmin

    replies = await _run(cmd_addadmin, OWNER_ID, "/addadmin", db, is_owner=True)
    assert "Usage" in replies[0]


async def test_removing_someone_who_is_not_an_admin_says_so(db):
    from dipbot.bot.handlers import cmd_removeadmin

    replies = await _run(cmd_removeadmin, OWNER_ID, f"/removeadmin {STRANGER_ID}", db, is_owner=True)
    assert "wasn't an admin" in replies[0]


async def test_admins_list_shows_owner_and_admins(db):
    from dipbot.bot.handlers import cmd_admins

    from dipbot.bot import handlers

    await db.add_admin(ADMIN_ID, "Bob", OWNER_ID)
    handlers.deps = handlers.Deps(db=db, dex=None, sender=None, owner_id=OWNER_ID)
    message = Recorder(ADMIN_ID, "/admins")
    await cmd_admins(message)

    text = message.replies[0]
    assert str(OWNER_ID) in text
    assert str(ADMIN_ID) in text
    assert "Bob" in text
    assert "cannot be removed" in text


# --- crediting whoever adds a coin ------------------------------------------


async def test_mention_prefers_a_username():
    from dipbot.bot.handlers import mention

    user = FakeUser(123, username="jman")
    assert mention(user) == "@jman"


async def test_mention_falls_back_to_a_clickable_link():
    """Not everyone has a username; a bare name would not identify them."""
    from dipbot.bot.handlers import mention

    user = FakeUser(123, username=None)
    user.full_name = "Marto"
    assert mention(user) == '<a href="tg://user?id=123">Marto</a>'


async def test_mention_escapes_html_in_names():
    """A display name is user-controlled and must not break the message."""
    from dipbot.bot.handlers import mention

    user = FakeUser(123, username=None)
    user.full_name = "<b>evil</b>"
    rendered = mention(user)
    assert "&lt;b&gt;evil&lt;/b&gt;" in rendered
    assert "<b>evil</b>" not in rendered


async def test_mention_falls_back_to_the_id():
    from dipbot.bot.handlers import mention

    user = FakeUser(123, username=None)
    user.full_name = None
    assert "123" in mention(user)
