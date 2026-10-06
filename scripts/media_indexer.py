#!/usr/bin/env python3
"""Index Telegram storage media for the lightweight website streaming worker."""

import os
from datetime import datetime, timezone

from pyrogram import Client
from supabase import create_client


MAX_SCAN_MESSAGES = int(os.environ.get("MEDIA_INDEX_MAX_MESSAGES", "5000"))
BATCH_SIZE = 100


def env(name, required=True):
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_KEY = env("SUPABASE_SERVICE_ROLE_KEY")
API_ID = int(env("API_ID"))
API_HASH = env("API_HASH")
BOT_TOKEN = env("BOT_TOKEN")
DB_CHANNEL = env("DB_CHANNEL", required=False)

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Client(
    "hj_media_indexer",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def configured_channels():
    response = (
        sb.table("bot_settings")
        .select("value")
        .eq("key", "storage_channels")
        .limit(1)
        .execute()
    )
    channels = []
    if response.data:
        raw = response.data[0].get("value") or "[]"
        import json
        try:
            values = json.loads(raw)
            if isinstance(values, list):
                channels = [int(x) for x in values]
        except (TypeError, ValueError, json.JSONDecodeError):
            channels = []
    if not channels and DB_CHANNEL:
        channels = [int(DB_CHANNEL)]
    return list(dict.fromkeys(channels))


def media_record(message, chat_id):
    if message.audio:
        m = message.audio
        return {
            "storage_chat_id": int(chat_id),
            "telegram_message_id": int(message.id),
            "media_kind": "audio",
            "file_id": str(m.file_id),
            "file_unique_id": str(m.file_unique_id or ""),
            "file_name": str(m.file_name or "audio.m4a"),
            "mime_type": str(m.mime_type or "audio/mp4"),
            "file_size": int(m.file_size or 0),
            "duration": int(m.duration or 0),
            "width": 0,
            "height": 0,
            "updated_at": now_iso(),
        }
    if message.video:
        m = message.video
        return {
            "storage_chat_id": int(chat_id),
            "telegram_message_id": int(message.id),
            "media_kind": "video",
            "file_id": str(m.file_id),
            "file_unique_id": str(m.file_unique_id or ""),
            "file_name": str(m.file_name or "video.mp4"),
            "mime_type": str(m.mime_type or "video/mp4"),
            "file_size": int(m.file_size or 0),
            "duration": int(m.duration or 0),
            "width": int(m.width or 0),
            "height": int(m.height or 0),
            "updated_at": now_iso(),
        }
    if message.document:
        m = message.document
        mime = str(m.mime_type or "").lower()
        kind = "audio" if mime.startswith("audio/") else "video" if mime.startswith("video/") else "document"
        return {
            "storage_chat_id": int(chat_id),
            "telegram_message_id": int(message.id),
            "media_kind": kind,
            "file_id": str(m.file_id),
            "file_unique_id": str(m.file_unique_id or ""),
            "file_name": str(m.file_name or "document"),
            "mime_type": str(m.mime_type or "application/octet-stream"),
            "file_size": int(m.file_size or 0),
            "duration": 0,
            "width": 0,
            "height": 0,
            "updated_at": now_iso(),
        }
    return None


async def main():
    channels = configured_channels()
    if not channels:
        raise RuntimeError("No storage channel configured in bot_settings or DB_CHANNEL.")

    total = 0
    async with app:
        for chat_id in channels:
            records = []
            seen = set()
            async for message in app.get_chat_history(chat_id, limit=MAX_SCAN_MESSAGES):
                record = media_record(message, chat_id)
                if not record:
                    continue
                key = (record["storage_chat_id"], record["telegram_message_id"], record["media_kind"])
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
                if len(records) >= BATCH_SIZE:
                    sb.table("telegram_media_index").upsert(
                        records,
                        on_conflict="storage_chat_id,telegram_message_id,media_kind",
                    ).execute()
                    total += len(records)
                    records = []
            if records:
                sb.table("telegram_media_index").upsert(
                    records,
                    on_conflict="storage_chat_id,telegram_message_id,media_kind",
                ).execute()
                total += len(records)

    print(f"[INDEX] Indexed {total} media records.", flush=True)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
