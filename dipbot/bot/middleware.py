"""Access gate.

Applied as an outer middleware so unauthorised traffic never reaches a handler.

Two roles:
  owner  - from OWNER_USER_ID. Can do everything, including managing admins.
           Never stored in the database and never removable, so an admin cannot
           lock the owner out.
  admin  - added by the owner. Full control of the watchlist and settings, but
           cannot add or remove other admins.

Permissions are read from the database on each update rather than cached. A
revoked admin therefore loses access immediately; a cache would leave a window
where a removed admin still worked, which is the wrong way for that to fail.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

log = logging.getLogger(__name__)

OWNER = "owner"
ADMIN = "admin"


class AccessMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int, db):
        self.owner_id = owner_id
        self.db = db
        self.rejected = 0

    @staticmethod
    async def _refuse(event: TelegramObject) -> None:
        """Tell an unauthorised user no, whatever kind of update this is.

        Detected by shape rather than isinstance: a callback query carries
        `data`, a message does not. Keeps the gate independent of aiogram's
        concrete classes, which also makes it testable without them.
        """
        answer = getattr(event, "answer", None)
        if answer is None:
            return
        try:
            if hasattr(event, "data"):
                await answer("Not allowed.", show_alert=True)
            else:
                await answer("This bot is private.")
        except Exception:  # pragma: no cover - refusing is best-effort
            log.debug("could not deliver refusal", exc_info=True)

    async def role_of(self, user_id: int) -> str | None:
        if user_id == self.owner_id:
            return OWNER
        if user_id in await self.db.admin_ids():
            return ADMIN
        return None

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)

        # Channel posts and service updates have no meaningful from_user.
        if user is None:
            return None

        role = await self.role_of(user.id)
        if role is None:
            self.rejected += 1
            log.info("rejected user %s (@%s)", user.id, user.username)
            await self._refuse(event)
            return None

        # Handlers use this to gate owner-only commands.
        data["role"] = role
        data["is_owner"] = role == OWNER
        return await handler(event, data)


#: Kept so existing imports and tests referring to the old name still work.
OwnerOnlyMiddleware = AccessMiddleware
