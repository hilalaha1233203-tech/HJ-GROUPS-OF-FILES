# (c) @PredatorHackerzZ

import asyncio
import datetime
from supabase import Client, create_client
from configs import Config


class _AsyncCursor:
    """Async iterator compatible with the original Mongo cursor usage."""

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

    def __init__(self, uri, database_name):
        if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be configured."
            )
        self.client: Client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
        self.table = "users"

    async def _execute(self, operation):
        return await asyncio.to_thread(operation)

    @staticmethod
    def _ban_status(row):
        return {
            "is_banned": bool(row.get("is_banned", False)),
            "ban_duration": int(row.get("ban_duration", 0)),
            "banned_on": row.get("banned_on") or datetime.date.max.isoformat(),
            "ban_reason": row.get("ban_reason", "") or "",
        }

    def new_user(self, id):
        return dict(
            id=int(id),
            join_date=datetime.date.today().isoformat(),
            is_banned=False,
            ban_duration=0,
            banned_on=datetime.date.max.isoformat(),
            ban_reason="",
        )

    async def add_user(self, id):
        await self._execute(
            lambda: self.client.table(self.table).upsert(
                self.new_user(id), on_conflict="id"
            ).execute()
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
            lambda: self.client.table(self.table).select("id").execute()
        )
        return len(response.data or [])

    async def get_all_users(self):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id,is_banned,ban_duration,banned_on,ban_reason,join_date")
            .order("id")
            .execute()
        )
        rows = []
        for row in response.data or []:
            row["ban_status"] = self._ban_status(row)
            rows.append(row)
        return _AsyncCursor(rows)

    async def delete_user(self, user_id):
        await self._execute(
            lambda: self.client.table(self.table)
            .delete()
            .eq("id", int(user_id))
            .execute()
        )

    async def remove_ban(self, id):
        await self._execute(
            lambda: self.client.table(self.table)
            .update(
                dict(
                    is_banned=False,
                    ban_duration=0,
                    banned_on=datetime.date.max.isoformat(),
                    ban_reason="",
                )
            )
            .eq("id", int(id))
            .execute()
        )

    async def ban_user(self, user_id, ban_duration, ban_reason):
        await self._execute(
            lambda: self.client.table(self.table)
            .update(
                dict(
                    is_banned=True,
                    ban_duration=int(ban_duration),
                    banned_on=datetime.date.today().isoformat(),
                    ban_reason=str(ban_reason),
                )
            )
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
        return self._ban_status(response.data[0])

    async def get_all_banned_users(self):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id,is_banned,ban_duration,banned_on,ban_reason,join_date")
            .eq("is_banned", True)
            .order("id")
            .execute()
        )
        rows = []
        for row in response.data or []:
            row["ban_status"] = self._ban_status(row)
            rows.append(row)
        return _AsyncCursor(rows)


db = Database(Config.SUPABASE_URL, Config.BOT_USERNAME)
