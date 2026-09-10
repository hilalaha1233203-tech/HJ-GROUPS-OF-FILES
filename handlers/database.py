# HJ GROUPS OF FILES - Supabase database adapter

import asyncio
import datetime
import json
from supabase import Client, create_client
from configs import Config


class _AsyncCursor:
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
    REQUEST_TIMEOUT = 8

    def __init__(self, uri=None, database_name=None):
        if not Config.SUPABASE_URL or not Config.SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be configured.")
        self.client: Client = create_client(Config.SUPABASE_URL, Config.SUPABASE_KEY)
        self.table = "users"
        self.settings_table = "bot_settings"

    async def _execute(self, operation, label="database operation"):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(operation),
                timeout=self.REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[SUPABASE] {label} timed out after {self.REQUEST_TIMEOUT}s")
            raise

    @staticmethod
    def _ban_status(row):
        legacy = row.get("ban_status") or {}
        return {
            "is_banned": bool(row.get("is_banned", legacy.get("is_banned", False))),
            "ban_duration": int(row.get("ban_duration", legacy.get("ban_duration", 0)) or 0),
            "banned_on": row.get("banned_on") or legacy.get("banned_on") or datetime.date.max.isoformat(),
            "ban_reason": row.get("ban_reason", legacy.get("ban_reason", "")) or "",
        }

    def new_user(self, id):
        return {
            "id": int(id),
            "join_date": datetime.date.today().isoformat(),
            "is_banned": False,
            "ban_duration": 0,
            "banned_on": datetime.date.max.isoformat(),
            "ban_reason": "",
        }

    async def add_user(self, id):
        try:
            await self._execute(
                lambda: self.client.table(self.table).upsert(
                    self.new_user(id), on_conflict="id"
                ).execute(),
                "add user",
            )
        except Exception as err:
            print(f"[USER_DB] Extended users schema unavailable, using legacy columns: {err}")
            await self._execute(
                lambda: self.client.table(self.table).upsert(
                    {"id": int(id), "join_date": datetime.date.today().isoformat()},
                    on_conflict="id",
                ).execute(),
                "add legacy user",
            )

    async def is_user_exist(self, id):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id")
            .eq("id", int(id))
            .limit(1)
            .execute(),
            "user existence check",
        )
        return bool(response.data)

    async def total_users_count(self):
        response = await self._execute(
            lambda: self.client.table(self.table).select("id").execute(),
            "total users count",
        )
        return len(response.data or [])

    async def get_all_users(self):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id,is_banned,ban_duration,banned_on,ban_reason,join_date")
            .order("id")
            .execute(),
            "all users",
        )
        rows = []
        for row in response.data or []:
            row["ban_status"] = self._ban_status(row)
            rows.append(row)
        return _AsyncCursor(rows)

    async def delete_user(self, user_id):
        await self._execute(
            lambda: self.client.table(self.table).delete().eq("id", int(user_id)).execute(),
            "delete user",
        )

    async def remove_ban(self, id):
        await self._execute(
            lambda: self.client.table(self.table)
            .update({
                "is_banned": False,
                "ban_duration": 0,
                "banned_on": datetime.date.max.isoformat(),
                "ban_reason": "",
            })
            .eq("id", int(id))
            .execute(),
            "remove ban",
        )

    async def ban_user(self, user_id, ban_duration, ban_reason):
        await self._execute(
            lambda: self.client.table(self.table)
            .update({
                "is_banned": True,
                "ban_duration": int(ban_duration),
                "banned_on": datetime.date.today().isoformat(),
                "ban_reason": str(ban_reason),
            })
            .eq("id", int(user_id))
            .execute(),
            "ban user",
        )

    async def get_ban_status(self, id):
        try:
            response = await self._execute(
                lambda: self.client.table(self.table)
                .select("is_banned,ban_duration,banned_on,ban_reason")
                .eq("id", int(id))
                .limit(1)
                .execute(),
                "ban status",
            )
            if not response.data:
                return {
                    "is_banned": False,
                    "ban_duration": 0,
                    "banned_on": datetime.date.max.isoformat(),
                    "ban_reason": "",
                }
            return self._ban_status(response.data[0])
        except Exception as err:
            print(f"[USER_DB] Ban columns unavailable: {err}")
            return {
                "is_banned": False,
                "ban_duration": 0,
                "banned_on": datetime.date.max.isoformat(),
                "ban_reason": "",
            }

    async def get_all_banned_users(self):
        response = await self._execute(
            lambda: self.client.table(self.table)
            .select("id,is_banned,ban_duration,banned_on,ban_reason,join_date")
            .eq("is_banned", True)
            .order("id")
            .execute(),
            "all banned users",
        )
        rows = []
        for row in response.data or []:
            row["ban_status"] = self._ban_status(row)
            rows.append(row)
        return _AsyncCursor(rows)

    async def _get_setting(self, key, default):
        try:
            response = await self._execute(
                lambda: self.client.table(self.settings_table)
                .select("value")
                .eq("key", key)
                .limit(1)
                .execute(),
                f"get setting {key}",
            )
            if response.data:
                return response.data[0]["value"]
        except Exception as err:
            print(f"Setting '{key}' unavailable, using default: {err}")
        return default

    async def _set_setting(self, key, value):
        await self._execute(
            lambda: self.client.table(self.settings_table)
            .upsert({"key": key, "value": str(value)}, on_conflict="key")
            .execute(),
            f"set setting {key}",
        )

    async def ensure_default_protection(self):
        """Enable saving/download protection for fresh installations.

        Existing explicit settings are respected, so an admin who intentionally
        disabled protection is not silently overridden on every restart.
        """
        try:
            marker = await self._get_setting("protect_download", None)
            if marker is None:
                await self._set_setting("protect_download", "true")
                print("[PROTECTION] Default saving/download protection enabled")
        except Exception as err:
            print(f"[PROTECTION] Could not initialize download protection: {err}")

    async def get_db_channel_id(self):
        # Supabase is the persistent source of truth. DB_CHANNEL is only a bootstrap
        # fallback so a stale Voroa environment variable cannot override saved channels.
        channels = await self.get_storage_channels()
        if channels:
            return channels[0]
        if Config.DB_CHANNEL:
            return int(Config.DB_CHANNEL)
        return None

    async def set_db_channel_id(self, channel_id):
        return await self.add_storage_channel(channel_id)

    async def get_storage_channels(self):
        raw = await self._get_setting("storage_channels", "[]")
        try:
            values = json.loads(raw) if raw else []
            if not isinstance(values, list):
                return []
            result = []
            for value in values:
                try:
                    channel_id = int(value)
                except (TypeError, ValueError):
                    continue
                if channel_id not in result:
                    result.append(channel_id)
            return result
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    async def set_storage_channels(self, channels):
        clean = []
        for value in channels:
            try:
                channel_id = int(value)
            except (TypeError, ValueError):
                continue
            if channel_id not in clean:
                clean.append(channel_id)
        await self._set_setting(
            "storage_channels",
            json.dumps(clean, separators=(",", ":")),
        )

    async def add_storage_channel(self, channel_id):
        channel_id = int(channel_id)
        channels = await self.get_storage_channels()
        if channel_id not in channels:
            channels.append(channel_id)
            await self.set_storage_channels(channels)
        return channels

    async def is_storage_channel(self, channel_id):
        return int(channel_id) in await self.get_storage_channels()

    async def get_auto_delete_seconds(self):
        try:
            return int(await self._get_setting("auto_delete_seconds", "1800"))
        except (TypeError, ValueError):
            return 1800

    async def set_auto_delete_seconds(self, seconds):
        await self._set_setting("auto_delete_seconds", int(seconds))

    async def get_protection_settings(self):
        return {
            "protect_forward": str(
                await self._get_setting("protect_forward", "false")
            ).lower() == "true",
            "protect_download": str(
                await self._get_setting("protect_download", "true")
            ).lower() == "true",
        }

    async def set_protection_setting(self, key, enabled):
        if key not in {"protect_forward", "protect_download"}:
            raise ValueError("Invalid protection setting")
        await self._set_setting(key, "true" if enabled else "false")

    async def get_protect_content(self):
        settings = await self.get_protection_settings()
        return settings["protect_forward"] or settings["protect_download"]


db = Database(Config.SUPABASE_URL, Config.BOT_USERNAME)
