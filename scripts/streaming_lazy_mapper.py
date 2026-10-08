#!/usr/bin/env python3
"""Lazily copy one already-indexed Telegram media mapping into HJ Web.

This maintenance-only tool:
- reads exactly one matching row from the Files repository media index;
- verifies the stored Bot API file_id with the official getFile method;
- writes one verified mapping row to HJ Web Supabase;
- never scans history, edits messages, uploads media, or deletes Telegram originals.
"""

import json
import os
from datetime import datetime, timezone

import requests
from supabase import create_client

MAX_BOT_API_DOWNLOAD_BYTES = 20 * 1024 * 1024

def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def expected_media_kind(content_kind: str) -> str:
    return {"audio": "audio", "video": "video", "book": "document"}[content_kind]

def main():
    files_url = required("FILES_SUPABASE_URL")
    files_key = required("FILES_SUPABASE_SERVICE_ROLE_KEY")
    web_url = required("WEB_SUPABASE_URL")
    web_key = required("WEB_SUPABASE_SERVICE_ROLE_KEY")
    bot_token = required("BOT_TOKEN")
    content_kind = required("CONTENT_KIND").lower()
    source_message_id = int(required("SOURCE_MESSAGE_ID"))

    if content_kind not in {"audio", "video", "book"}:
        raise RuntimeError("CONTENT_KIND must be audio, video, or book.")
    if source_message_id <= 0:
        raise RuntimeError("SOURCE_MESSAGE_ID must be > 0.")

    media_kind = expected_media_kind(content_kind)
    files = create_client(files_url, files_key)
    web = create_client(web_url, web_key)

    # Exact single-row lookup only. No history scan.
    indexed = (files.table("telegram_media_index")
               .select("telegram_message_id,media_kind,file_id,file_unique_id,file_name,mime_type,file_size,duration,width,height")
               .eq("telegram_message_id", source_message_id)
               .eq("media_kind", media_kind)
               .limit(1).execute())
    rows = indexed.data or []
    if not rows:
        raise RuntimeError("No exact Telegram media index row was found; no mapping was written.")
    source = rows[0]

    file_id = str(source.get("file_id") or "").strip()
    if not file_id:
        raise RuntimeError("Indexed row has no file_id; no mapping was written.")

    file_size = int(source.get("file_size") or 0)
    if file_size <= 0:
        raise RuntimeError("Indexed row has no valid file_size; no mapping was written.")
    if file_size > MAX_BOT_API_DOWNLOAD_BYTES:
        raise RuntimeError("This source exceeds the official 20 MB Bot API getFile download limit; use the preservation-safe split workflow.")

    # Verify that this file_id is still accepted by the same Bot API that production uses.
    response = requests.post(
        f"https://api.telegram.org/bot{bot_token}/getFile",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={"file_id": file_id},
        timeout=30,
    )
    payload = response.json()
    if response.status_code != 200 or not payload.get("ok") or not payload.get("result", {}).get("file_path"):
        raise RuntimeError("Telegram Bot API file_id verification failed; no mapping was written.")

    existing = (web.table("streaming_media_sources").select("id")
                .eq("media_kind", media_kind)
                .eq("original_telegram_message_id", source_message_id)
                .limit(1).execute())
    if existing.data:
        raise RuntimeError("A mapping already exists for this Telegram message; no duplicate mapping was written.")

    table = {"audio": "episodes", "video": "video_episodes", "book": "books"}[content_kind]
    content = (web.table(table).select("id,telegram_message_id")
               .eq("telegram_message_id", source_message_id).limit(1).execute())
    if not content.data:
        raise RuntimeError(f"No HJ Web {table} row is linked to this Telegram message.")

    now = datetime.now(timezone.utc).isoformat()
    mapping = {
        "content_kind": content_kind,
        "content_id": int(content.data[0]["id"]),
        "source_group": None,
        "part_index": 0,
        "part_count": 1,
        "original_telegram_message_id": source_message_id,
        "source_telegram_message_id": source_message_id,
        "media_kind": media_kind,
        "file_id": file_id,
        "file_unique_id": str(source.get("file_unique_id") or ""),
        "file_name": str(source.get("file_name") or ("book.pdf" if media_kind == "document" else "audio.m4a")),
        "mime_type": str(source.get("mime_type") or "application/octet-stream"),
        "file_size": file_size,
        "assembled_file_size": file_size,
        "duration": int(source.get("duration") or 0),
        "width": int(source.get("width") or 0),
        "height": int(source.get("height") or 0),
        "verified_at": now,
        "updated_at": now,
    }
    inserted = web.table("streaming_media_sources").insert(mapping).execute()
    if not inserted.data:
        raise RuntimeError("HJ Web mapping insert returned no row.")

    check = (web.table("streaming_media_sources")
             .select("original_telegram_message_id,source_telegram_message_id,file_id,file_size,assembled_file_size")
             .eq("media_kind", media_kind)
             .eq("original_telegram_message_id", source_message_id)
             .eq("part_index", 0).limit(1).execute())
    if not check.data or str(check.data[0]["file_id"]) != file_id:
        raise RuntimeError("Post-write mapping verification failed.")

    print(json.dumps({
        "status": "VERIFIED",
        "content_kind": content_kind,
        "content_id": int(content.data[0]["id"]),
        "original_message_id": source_message_id,
        "source_message_id": source_message_id,
        "file_size": file_size,
    }, separators=(",", ":")))

if __name__ == "__main__":
    main()