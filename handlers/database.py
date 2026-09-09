# (c) @PredatorHackerzZ

import asyncio
import datetime
from supabase import Client, create_client
from configs import Config


class _AsyncCursor:
    """Small async iterator compatible with the original broadcast handler."""

    def __init__(self, rows):
        self._rows = iter(rows or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration


class Database:

    def __init__(self, uri=None, database_name=None):
        # Keep the original constructor signature so existing handlers do not need changes.
        self.url = Config.SUPABASE_URL
        self.key = Config.SUPABASE_KEY

        if not self.url or not self.key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) "
                "must be configured."
            )

        self.client: Client = create_client(self.url, self.key)
        self.table = "users"

    async def _execute(self, operation):
        return await asyncio.to_thread(operation)

    @staticmethod
    def new_user(id):
        return dict(
            id=int(id),
            join_date=datetime.date.today().isoformat(),
            is_banned=False,
            ban_duration=0,
            banned_on=datetime.date.max.isoformat(),
            ban_reason="",
        )

    async def add_user(self, id):
        user = self.new_user(id)
        await self._execute(
            lambda: self.client.table(self.table).upsert(user, on_conflict="id").execute()
        )

    async def is_user_exist(self, id):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id")
            .eq("id", int(id))
            .limit(1)
            .execute()
        )
        return bool(response.data)

    async def total_users_count(self):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id")
            .execute()
        )
        return len(response.data or [])

    async def get_all_users(self):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id,is_banned,ban_duration,banned_on,ban_reason,join_date")
            .order("id")
            .execute()
        )
        return _AsyncCursor(response.data or [])

    async def delete_user(self, user_id):
        await self._execute(
            lambda: self.client.table(self.table).delete().eq("id", int(user_id)).execute()
        )

    async def remove_ban(self, id):
        ban_status = dict(
            is_banned=False,
            ban_duration=0,
            banned_on=datetime.date.max.isoformat(),
            ban_reason="",
        )
        await self._execute(
            lambda: self.client.table(self.table)
            .update(ban_status)
            .eq("id", int(id))
            .execute()
        )

    async def ban_user(self, user_id, ban_duration, ban_reason):
        ban_status = dict(
            is_banned=True,
            ban_duration=int(ban_duration),
            banned_on=datetime.date.today().isoformat(),
            ban_reason=str(ban_reason),
        )
        await self._execute(
            lambda: self.client.table(self.table)
            .update(ban_status)
            .eq("id", int(user_id))
            .execute()
        )

    async def get_ban_status(self, id):
        default = dict(
            is_banned=False,
            ban_duration=0,
            banned_on=datetime.date.max.isoformat(),
            ban_reason="",
        )
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("is_banned,ban_duration,banned_on,ban_reason")
            .eq("id", int(id))
            .limit(1)
            .execute()
        )
        if not response.data:
            return default
        row = response.data[0]
        return {
            "is_banned": bool(row.get("is_banned", False)),
            "ban_duration": int(row.get("ban_duration", 0)),
            "banned_on": row.get("banned_on") or datetime.date.max.isoformat(),
            "ban_reason": row.get("ban_reason", "") or "",
        }

    async def get_all_banned_users(self):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id,is_banned,ban_duration,banned_on,ban_reason,join_date")
            .eq("is_banned", True)
            .order("id")
            .execute()
        )
        return _AsyncCursor(response.data or [])


db = Database(Config.SUPABASE_URL, Config.BOT_USERNAME)
