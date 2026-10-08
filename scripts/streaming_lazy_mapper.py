#!/usr/bin/env python3
"""Verify and lazily map one Telegram media message to the HJ Web streaming map.

This maintenance tool is intentionally single-item only. It never scans history,
never edits/replaces the original Telegram message, and never deletes Telegram
media. Files above the official Bot API getFile download limit are rejected here
until the preservation-safe split/reassembly workflow is implemented.
"""

import os
from datetime import datetime, timezone

from pyrogram import Client
from supabase import create_client


MAX_BOT_API_DOWNLOAD_BYTES = 20 * 1024 * 1024


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def media_info(message):
    if getattr(message, "audio", None):
        m = message.audio
        return {
            "kind": "audio",
            "file_id": str(m.file_id),
            "file_unique_id": str(m.file_unique_id or ""),
            "file_name": str(m.file_name or "audio.m4a"),
            "mime_type": str(m.mime_type or "audio/mp4"),
            "file_size": int(m.file_size or 0),
            "duration": int(m.duration or 0),
            "width": 0,
            "height": 0,
        }
    if getattr(message, "video", None):
        m = message.video
        return {
            "kind": "video",
            "file_id": str(m.file_id),
            "file_unique_id": str(m.file_unique_id or ""),
            "file_name": str(m.file_name or "video.mp4"),
            "mime_type": str(m.mime_type or "video/mp4"),
            "file_size": int(m.file_size or 0),
            "duration": int(m.duration or 0),
            "width": int(m.width or 0),
            "height": int(m.height or 0),
        }
    if getattr(message, "document", None):
        m = message.document
        mime = str(m.mime_type or "").lower()
        kind = (
            "audio"
            if mime.startswith("audio/")
            else "video"
            if mime.startswith("video/")
            else "document"
        )
        return {
            "kind": kind,
            "file_id": str(m.file_id),
            "file_unique_id": str(m.file_unique_id or ""),
            "file_name": str(m.file_name or "document"),
            "mime_type": str(m.mime_type or "application/octet-stream"),
            "file_size": int(m.file_size or 0),
            "duration": 0,
            "width": 0,
            "height": 0,
        }
    return None


def content_lookup_table(content_kind):
    if content_kind == "audio":
        return "episodes"
    if content_kind == "video":
        return "video_episodes"
    return "books"


def main():
    api_id = int(required("API_ID"))
    api_hash = required("API_HASH")
    bot_token = required("BOT_TOKEN")
    source_chat_id = int(required("SOURCE_CHAT_ID"))
    source_message_id = int(required("SOURCE_MESSAGE_ID"))
    content_kind = required("CONTENT_KIND").lower()
    web_url = required("WEB_SUPABASE_URL")
    web_key = required("WEB_SUPABASE_SERVICE_ROLE_KEY")

    if content_kind not in {"audio", "video", "book"}:
        raise RuntimeError("CONTENT_KIND must be audio, video, or book.")
    if source_message_id <= 0:
        raise RuntimeError("SOURCE_MESSAGE_ID must be > 0.")

    sb = create_client(web_url, web_key)

    app = Client(
        "hj_streaming_mapper",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
    )

    with app:
        message = app.get_messages(source_chat_id, source_message_id)

    if not message:
        raise RuntimeError(f"Telegram message {source_message_id} was not found.")

    info = media_info(message)
    if not info:
        raise RuntimeError("Telegram message has no supported audio/video/document media.")

    expected_media_kind = {"audio": "audio", "video": "video", "book": "document"}[content_kind]
    if info["kind"] != expected_media_kind:
        raise RuntimeError(
            f"Media kind mismatch: CONTENT_KIND={content_kind} but Telegram media is {info['kind']}."
        )

    if info["file_size"] > MAX_BOT_API_DOWNLOAD_BYTES:
        raise RuntimeError(
            f"Message {source_message_id} is {info['file_size']} bytes (>20 MB). "
            "Use the future preservation-safe split workflow; no mapping was written."
        )

    table = content_lookup_table(content_kind)
    row_response = (
        sb.table(table)
        .select("id,telegram_message_id")
        .eq("telegram_message_id", source_message_id)
        .limit(1)
        .execute()
    )
    rows = row_response.data or []
    if not rows:
        raise RuntimeError(
            f"No HJ Web {table} row is linked to Telegram message {source_message_id}; "
            "no mapping was written."
        )

    content_id = int(rows[0]["id"])
    now = datetime.now(timezone.utc).isoformat()

    mapping = {
        "content_kind": content_kind,
        "content_id": content_id,
        "source_group": None,
        "part_index": 0,
        "part_count": 1,
        "original_telegram_message_id": source_message_id,
        "source_telegram_message_id": source_message_id,
        "media_kind": info["kind"],
        "file_id": info["file_id"],
        "file_unique_id": info["file_unique_id"],
        "file_name": info["file_name"],
        "mime_type": info["mime_type"],
        "file_size": info["file_size"],
        "assembled_file_size": info["file_size"],
        "duration": info["duration"],
        "width": info["width"],
        "height": info["height"],
        "verified_at": now,
        "updated_at": now,
    }

    result = (
        sb.table("streaming_media_sources")
        .upsert(
            mapping,
            on_conflict="media_kind,original_telegram_message_id,part_index",
        )
        .execute()
    )
    if not result.data:
        raise RuntimeError("Supabase mapping write returned no row.")

    check = (
        sb.table("streaming_media_sources")
        .select(
            "content_kind,content_id,original_telegram_message_id,"
            "source_telegram_message_id,media_kind,file_id,file_size,assembled_file_size"
        )
        .eq("media_kind", info["kind"])
        .eq("original_telegram_message_id", source_message_id)
        .eq("part_index", 0)
        .limit(1)
        .execute()
    )
    if not check.data:
        raise RuntimeError("Post-write mapping verification failed.")

    verified = check.data[0]
    if str(verified.get("file_id")) != info["file_id"]:
        raise RuntimeError("Post-write file_id verification failed.")

    print(
        "STREAMING_MAPPING=VERIFIED",
        f"content_kind={content_kind}",
        f"content_id={content_id}",
        f"original_message_id={source_message_id}",
        f"source_message_id={source_message_id}",
        f"size={info['file_size']}",
        sep=" | ",
        flush=True,
    )


if __name__ == "__main__":
    main()
