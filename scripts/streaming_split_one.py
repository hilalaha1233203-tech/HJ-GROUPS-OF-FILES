#!/usr/bin/env python3
"""Split one large Telegram media message into <=19 MiB Telegram document parts.

This is an ephemeral, manual maintenance operation:
- exactly one source Telegram message per run;
- uses an existing Telegram user session only for the download;
- never edits, replaces, or deletes the original message;
- uploads new chunk messages to the configured storage channel;
- writes only the verified chunk file_ids into the HJ Web streaming map.
"""

import asyncio
import os
import tempfile
import uuid
from pathlib import Path

from pyrogram import Client
from supabase import create_client

CHUNK_BYTES = 19 * 1024 * 1024

def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def media_info(message):
    if getattr(message, "audio", None):
        media = message.audio
        return {"kind":"audio","file_id":str(media.file_id),"file_unique_id":str(media.file_unique_id or ""),
                "file_name":str(media.file_name or "audio.m4a"),"mime_type":str(media.mime_type or "audio/mp4"),
                "file_size":int(media.file_size or 0),"duration":int(media.duration or 0),"width":0,"height":0}
    if getattr(message, "video", None):
        media = message.video
        return {"kind":"video","file_id":str(media.file_id),"file_unique_id":str(media.file_unique_id or ""),
                "file_name":str(media.file_name or "video.mp4"),"mime_type":str(media.mime_type or "video/mp4"),
                "file_size":int(media.file_size or 0),"duration":int(media.duration or 0),
                "width":int(media.width or 0),"height":int(media.height or 0)}
    if getattr(message, "document", None):
        media = message.document
        mime = str(media.mime_type or "").lower()
        kind = "audio" if mime.startswith("audio/") else "video" if mime.startswith("video/") else "document"
        return {"kind":kind,"file_id":str(media.file_id),"file_unique_id":str(media.file_unique_id or ""),
                "file_name":str(media.file_name or "document"),"mime_type":str(media.mime_type or "application/octet-stream"),
                "file_size":int(media.file_size or 0),"duration":0,"width":0,"height":0}
    return None

def expected_media_kind(content_kind: str) -> str:
    return {"audio":"audio","video":"video","book":"document"}[content_kind]

async def download_source(api_id, api_hash, session_string, chat_id, message_id, path):
    app = Client("hj_streaming_split_downloader", api_id=api_id, api_hash=api_hash,
                 session_string=session_string, in_memory=True, no_updates=True)
    await app.start()
    try:
        me = await app.get_me()
        if getattr(me, "is_bot", False):
            raise RuntimeError("TELEGRAM_USER_SESSION_STRING belongs to a bot; a personal Telegram user session is required.")
        message = await app.get_messages(chat_id, message_id)
        if not message:
            raise RuntimeError(f"Telegram message {message_id} was not found.")
        info = media_info(message)
        if not info:
            raise RuntimeError("Source message has no supported media.")
        downloaded = await app.download_media(message, file_name=str(path))
        if not downloaded:
            raise RuntimeError("Telegram user-session download returned no file.")
        downloaded_path = Path(downloaded)
        if not downloaded_path.exists():
            raise RuntimeError("Downloaded source file does not exist.")
        return downloaded_path, info
    finally:
        await app.stop()

async def upload_parts(api_id, api_hash, bot_token, target_chat_id, parts):
    app = Client("hj_streaming_split_uploader", api_id=api_id, api_hash=api_hash,
                 bot_token=bot_token, in_memory=True, no_updates=True)
    await app.start()
    uploaded = []
    try:
        for index, part_path in enumerate(parts):
            sent = await app.send_document(target_chat_id, document=str(part_path),
                                           file_name=part_path.name, disable_notification=True)
            if not sent or not getattr(sent, "document", None):
                raise RuntimeError(f"Telegram upload returned no document for part {index}.")
            verified = await app.get_messages(target_chat_id, int(sent.id))
            if not verified or not getattr(verified, "document", None):
                raise RuntimeError(f"Could not re-read uploaded part {index}.")
            doc = verified.document
            file_id = str(doc.file_id or "")
            file_unique_id = str(doc.file_unique_id or "")
            file_size = int(doc.file_size or 0)
            if not file_id:
                raise RuntimeError(f"Uploaded part {index} has no Telegram file_id.")
            if file_size <= 0 or file_size > CHUNK_BYTES:
                raise RuntimeError(f"Uploaded part {index} has invalid size {file_size}; expected <= {CHUNK_BYTES}.")
            uploaded.append({"message_id":int(verified.id),"file_id":file_id,
                             "file_unique_id":file_unique_id,"file_size":file_size})
            print(f"[UPLOAD] part={index} verified message_id={verified.id} size={file_size}", flush=True)
    except Exception:
        try:
            ids = [item["message_id"] for item in uploaded]
            if ids:
                await app.delete_messages(target_chat_id, ids)
                print(f"[CLEANUP] Removed {len(ids)} newly-created chunk messages after upload failure.", flush=True)
        except Exception as cleanup_error:
            print(f"[CLEANUP] Could not remove all newly-created chunk messages: {cleanup_error}", flush=True)
        raise
    finally:
        await app.stop()
    return uploaded

async def map_parts(web_url, web_key, content_kind, original_message_id, original_info, uploaded):
    sb = create_client(web_url, web_key)
    existing = (sb.table("streaming_media_sources").select("id")
                .eq("media_kind", original_info["kind"])
                .eq("original_telegram_message_id", original_message_id).limit(1).execute())
    if existing.data:
        raise RuntimeError("A streaming mapping already exists for this original Telegram message.")
    table = {"audio":"episodes","video":"video_episodes","book":"books"}[content_kind]
    content = (sb.table(table).select("id,telegram_message_id")
               .eq("telegram_message_id", original_message_id).limit(1).execute())
    if not content.data:
        raise RuntimeError(f"No HJ Web {table} row is linked to original Telegram message {original_message_id}.")
    content_id = int(content.data[0]["id"])
    source_group = f"split-{original_message_id}-{uuid.uuid4().hex}"
    part_count = len(uploaded)
    assembled_size = int(original_info["file_size"])
    if sum(int(item["file_size"]) for item in uploaded) != assembled_size:
        raise RuntimeError("Uploaded chunk sizes do not equal the original downloaded file size.")
    rows=[]
    for index,item in enumerate(uploaded):
        rows.append({"content_kind":content_kind,"content_id":content_id,"source_group":source_group,
                     "part_index":index,"part_count":part_count,
                     "original_telegram_message_id":original_message_id,
                     "source_telegram_message_id":item["message_id"],"media_kind":original_info["kind"],
                     "file_id":item["file_id"],"file_unique_id":item["file_unique_id"],
                     "file_name":original_info["file_name"],"mime_type":original_info["mime_type"],
                     "file_size":item["file_size"],"assembled_file_size":assembled_size,
                     "duration":original_info["duration"],"width":original_info["width"],
                     "height":original_info["height"]})
    insert = sb.table("streaming_media_sources").insert(rows).execute()
    if len(insert.data or []) != len(rows):
        raise RuntimeError("Supabase mapping insert did not return every chunk row.")
    check = (sb.table("streaming_media_sources")
             .select("part_index,part_count,original_telegram_message_id,source_telegram_message_id,file_id,file_size,assembled_file_size")
             .eq("media_kind", original_info["kind"]).eq("original_telegram_message_id", original_message_id)
             .order("part_index").limit(200).execute())
    rows_after = check.data or []
    if len(rows_after) != part_count:
        raise RuntimeError(f"Post-write mapping expected {part_count} rows; got {len(rows_after)}.")
    for index,(expected,actual) in enumerate(zip(uploaded,rows_after)):
        if int(actual["part_index"]) != index or int(actual["source_telegram_message_id"]) != int(expected["message_id"]):
            raise RuntimeError("Post-write mapping source/order verification failed.")
        if str(actual["file_id"]) != str(expected["file_id"]):
            raise RuntimeError("Post-write Telegram file_id verification failed.")
        if int(actual["file_size"]) != int(expected["file_size"]):
            raise RuntimeError("Post-write chunk size verification failed.")
        if int(actual["assembled_file_size"]) != assembled_size:
            raise RuntimeError("Post-write assembled size verification failed.")
    return source_group, content_id

async def main():
    api_id=int(required("API_ID")); api_hash=required("API_HASH")
    user_session=required("TELEGRAM_USER_SESSION_STRING"); bot_token=required("BOT_TOKEN")
    source_chat_id=int(required("SOURCE_CHAT_ID")); source_message_id=int(required("SOURCE_MESSAGE_ID"))
    target_chat_id=int(required("TARGET_STORAGE_CHAT_ID"))
    web_url=required("WEB_SUPABASE_URL"); web_key=required("WEB_SUPABASE_SERVICE_ROLE_KEY")
    content_kind=required("CONTENT_KIND").lower()
    if content_kind not in {"audio","video","book"}: raise RuntimeError("CONTENT_KIND must be audio, video, or book.")
    if source_message_id <= 0: raise RuntimeError("SOURCE_MESSAGE_ID must be > 0.")

    with tempfile.TemporaryDirectory(prefix="hj-stream-split-") as tmp:
        source_path=Path(tmp)/"source.bin"
        downloaded_path,source_info=await download_source(api_id,api_hash,user_session,source_chat_id,source_message_id,source_path)
        if source_info["kind"] != expected_media_kind(content_kind):
            raise RuntimeError("Content/media kind mismatch: requested {}, Telegram source is {}.".format(content_kind, source_info["kind"]))
        actual_size=downloaded_path.stat().st_size
        if actual_size != int(source_info["file_size"]): raise RuntimeError("Downloaded source size mismatch.")
        if actual_size <= CHUNK_BYTES: raise RuntimeError("Source does not require splitting; use the lazy single-message mapper.")
        web=create_client(web_url,web_key)
        existing=(web.table("streaming_media_sources").select("id").eq("media_kind",source_info["kind"]).eq("original_telegram_message_id",source_message_id).limit(1).execute())
        if existing.data: raise RuntimeError("A mapping already exists; no Telegram uploads were attempted.")
        parts=[]
        with downloaded_path.open("rb") as source:
            index=0
            while True:
                chunk=source.read(CHUNK_BYTES)
                if not chunk: break
                part_path=Path(tmp) / ("{}.part-{:04d}".format(Path(source_info["file_name"]).name, index + 1))
                part_path.write_bytes(chunk)
                if part_path.stat().st_size<=0 or part_path.stat().st_size>CHUNK_BYTES: raise RuntimeError("Created chunk has invalid size.")
                parts.append(part_path); index += 1
        print(f"[SPLIT] source_message_id={source_message_id} original_bytes={actual_size} parts={len(parts)} max_part_bytes={CHUNK_BYTES}",flush=True)
        uploaded=await upload_parts(api_id,api_hash,bot_token,target_chat_id,parts)
        try:
            source_group,content_id=await map_parts(web_url,web_key,content_kind,source_message_id,source_info,uploaded)
        except Exception:
            try:
                cleanup=Client("hj_streaming_split_cleanup",api_id=api_id,api_hash=api_hash,bot_token=bot_token,in_memory=True,no_updates=True)
                await cleanup.start()
                try: await cleanup.delete_messages(target_chat_id,[int(item["message_id"]) for item in uploaded])
                finally: await cleanup.stop()
                print("[CLEANUP] Removed uploaded derivative chunks after mapping failure.",flush=True)
            except Exception as cleanup_error:
                print(f"[CLEANUP] Derivative cleanup failed (original message untouched): {cleanup_error}",flush=True)
            raise
        print(f"STREAMING_SPLIT=VERIFIED | content_kind={content_kind} | content_id={content_id} | original_message_id={source_message_id} | source_group={source_group} | parts={len(uploaded)} | assembled_bytes={actual_size}",flush=True)

if __name__ == "__main__":
    asyncio.run(main())