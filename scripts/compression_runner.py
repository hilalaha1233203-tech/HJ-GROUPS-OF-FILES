#!/usr/bin/env python3
"""Process one or more HJ compression jobs on an ephemeral GitHub runner."""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from pyrogram import Client
from pyrogram.types import InputMediaAudio, InputMediaDocument, InputMediaVideo
from pyrogram.errors import FloodWait
from supabase import create_client


MAX_FILE_BYTES = 500 * 1024 * 1024
JOB_MAX_AGE_MINUTES = 360
DEFAULT_MAX_JOBS = 10
MIN_AUDIO_KBPS = 24
MAX_AUDIO_KBPS = 160


def env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


SUPABASE_URL = ""
SUPABASE_KEY = ""
API_ID = 0
API_HASH = ""
BOT_TOKEN = ""
sb = None
app = None


def init_runtime():
    global SUPABASE_URL, SUPABASE_KEY, API_ID, API_HASH, BOT_TOKEN, sb, app
    SUPABASE_URL = env("SUPABASE_URL")
    SUPABASE_KEY = env("SUPABASE_SERVICE_ROLE_KEY")
    API_ID = int(env("API_ID"))
    API_HASH = env("API_HASH")
    BOT_TOKEN = env("BOT_TOKEN")
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    app = Client(
        "hj_compression_runner",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    )

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def human_size(value):
    value = int(value or 0)
    if value < 1024:
        return f"{value} B"
    if value < 1024 ** 2:
        return f"{value / 1024:.1f} KB"
    if value < 1024 ** 3:
        return f"{value / 1024 ** 2:.2f} MB"
    return f"{value / 1024 ** 3:.2f} GB"


def run_checked(command, timeout=1200):
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def media_info(message):
    if getattr(message, "audio", None):
        m = message.audio
        return {
            "kind": "audio",
            "size": int(m.file_size or 0),
            "name": m.file_name or "audio.m4a",
            "mime": m.mime_type or "audio/mp4",
            "duration": int(m.duration or 0),
            "width": 0,
            "height": 0,
            "file_id": m.file_id,
            "file_unique_id": m.file_unique_id,
        }
    if getattr(message, "video", None):
        m = message.video
        return {
            "kind": "video",
            "size": int(m.file_size or 0),
            "name": m.file_name or "video.mp4",
            "mime": m.mime_type or "video/mp4",
            "duration": int(m.duration or 0),
            "width": int(m.width or 0),
            "height": int(m.height or 0),
            "file_id": m.file_id,
            "file_unique_id": m.file_unique_id,
        }
    if getattr(message, "document", None):
        m = message.document
        mime = (m.mime_type or "").lower()
        kind = "audio" if mime.startswith("audio/") else "video" if mime.startswith("video/") else "document"
        return {
            "kind": kind,
            "size": int(m.file_size or 0),
            "name": m.file_name or "document",
            "mime": m.mime_type or "application/octet-stream",
            "duration": 0,
            "width": 0,
            "height": 0,
            "file_id": m.file_id,
            "file_unique_id": m.file_unique_id,
        }
    return None


def fetch_jobs(limit):
    """Recover genuinely stale jobs, then atomically claim pending jobs.

    The compare-and-swap style status update prevents two independent GitHub
    runners/manual dispatches from processing the same job concurrently.
    """
    stale_before = (datetime.now(timezone.utc) - timedelta(minutes=JOB_MAX_AGE_MINUTES)).isoformat()
    try:
        sb.table("compression_jobs").update({
            "status": "pending",
            "last_error": "Recovered stale runner job",
            "started_at": None,
            "completed_at": None,
            "updated_at": now_iso(),
        }).eq("status", "running").lt("updated_at", stale_before).execute()
    except Exception as exc:
        print(f"[RECOVER] {exc}", flush=True)

    response = (
        sb.table("compression_jobs")
        .select("id")
        .eq("status", "pending")
        .order("created_at")
        .limit(limit)
        .execute()
    )
    jobs = []
    for row in response.data or []:
        job_id = int(row["id"])
        try:
            claimed = (
                sb.table("compression_jobs")
                .update({
                    "status": "running",
                    "started_at": now_iso(),
                    "completed_at": None,
                    "updated_at": now_iso(),
                    "last_error": "",
                })
                .eq("id", job_id)
                .eq("status", "pending")
                .select("*")
                .execute()
            )
            if claimed.data:
                jobs.append(claimed.data[0])
            else:
                print(f"[CLAIM] Job {job_id} was already claimed.", flush=True)
        except Exception as exc:
            print(f"[CLAIM] Job {job_id} could not be claimed: {exc}", flush=True)
    return jobs


def set_job(job_id, **values):
    values["updated_at"] = now_iso()
    sb.table("compression_jobs").update(values).eq("id", int(job_id)).execute()


async def update_owner_status(job, text):
    chat_id = int(job.get("requested_chat_id") or job.get("requested_by"))
    message_id = job.get("status_message_id")
    if not message_id or not chat_id:
        return
    try:
        await app.edit_message_text(chat_id, int(message_id), text)
    except FloodWait as exc:
        await asyncio_sleep(exc.value)
    except Exception as exc:
        print(f"[STATUS] {exc}", flush=True)


async def _call_with_floodwait(operation, label, retries=5):
    for attempt in range(retries):
        try:
            return await operation()
        except FloodWait as exc:
            if attempt >= retries - 1:
                raise
            delay = max(1, int(exc.value))
            print(f"[FLOODWAIT] {label}: sleeping {delay}s", flush=True)
            await asyncio_sleep(delay)
    raise RuntimeError(f"{label} failed after retries")

def asyncio_sleep(seconds):
    import asyncio
    return asyncio.sleep(max(1, int(seconds)))


def target_bytes(job):
    target = float(job.get("target_mb") or 19)
    return int(target * 1024 * 1024)


def audio_bitrate_kbps(target, duration):
    if duration <= 0:
        return 96
    # Leave container overhead and metadata headroom.
    raw = (target * 8 * 1000 * 0.90) / duration
    return max(MIN_AUDIO_KBPS, min(MAX_AUDIO_KBPS, int(raw)))


def video_bitrate_kbps(target, duration):
    if duration <= 0:
        return 600
    raw = (target * 8 * 1000 * 0.88) / duration
    audio = 48 if raw < 120 else 64
    video = int(raw - audio)
    return max(60, min(3500, video))


def compress_audio(src, dst, target, duration):
    bitrate = audio_bitrate_kbps(target, duration)
    attempts = [bitrate, max(MIN_AUDIO_KBPS, int(bitrate * 0.78)), max(MIN_AUDIO_KBPS, int(bitrate * 0.60))]
    last = None
    for rate in attempts:
        try:
            run_checked([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", src,
                "-vn", "-c:a", "aac", "-b:a", f"{rate}k",
                "-movflags", "+faststart",
                dst,
            ])
            if Path(dst).exists():
                size = Path(dst).stat().st_size
                if size <= target or rate == attempts[-1]:
                    return size
            Path(dst).unlink(missing_ok=True)
        except subprocess.CalledProcessError as exc:
            last = exc
            Path(dst).unlink(missing_ok=True)
    if last:
        raise RuntimeError("FFmpeg audio compression failed") from last
    raise RuntimeError("Audio target could not be reached")


def compress_video(src, dst, target, duration, width, height):
    video_rate = video_bitrate_kbps(target, duration)
    attempts = [video_rate, max(60, int(video_rate * 0.78)), max(60, int(video_rate * 0.58))]
    last = None
    for rate in attempts:
        scale_filter = None
        if rate < 700:
            max_width = 640 if rate >= 300 else 480
            scale_filter = f"scale='min({max_width},iw)':-2,fps=24"
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{rate}k", "-maxrate", f"{rate}k", "-bufsize", f"{max(120, rate * 2)}k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "48k",
            "-movflags", "+faststart",
        ]
        if scale_filter:
            command += ["-vf", scale_filter]
        command += [dst]
        try:
            run_checked(command, timeout=1800)
            if Path(dst).exists():
                size = Path(dst).stat().st_size
                if size <= target or rate == attempts[-1]:
                    return size
            Path(dst).unlink(missing_ok=True)
        except subprocess.CalledProcessError as exc:
            last = exc
            Path(dst).unlink(missing_ok=True)
    if last:
        raise RuntimeError("FFmpeg video compression failed") from last
    raise RuntimeError("Video target could not be reached")


def optimize_document(src, dst, name):
    lower = name.lower()
    if lower.endswith(".pdf"):
        run_checked([
            "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
            "-dPDFSETTINGS=/ebook", "-dNOPAUSE", "-dQUIET", "-dBATCH",
            f"-sOutputFile={dst}", src,
        ], timeout=900)
        return Path(dst).stat().st_size
    if lower.endswith((".zip", ".epub", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp")):
        import zipfile
        with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(
            dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zout:
            for item in zin.infolist():
                if item.is_dir():
                    zout.writestr(item, b"")
                else:
                    zout.writestr(item, zin.read(item.filename))
        return Path(dst).stat().st_size
    raise RuntimeError("Safe document optimisation is unavailable for this file type")


def make_output_name(info):
    name = info["name"]
    stem = Path(name).stem or "media"
    if info["kind"] == "audio":
        return stem + ".m4a"
    if info["kind"] == "video":
        return stem + ".mp4"
    return name


def build_input_media(info, out_path, source):
    caption = source.caption or None
    if info["kind"] == "audio":
        audio = source.audio or source.document
        return InputMediaAudio(
            out_path,
            caption=caption,
            caption_entities=list(source.caption_entities or []),
            parse_mode=None,
            duration=int(getattr(audio, "duration", 0) or 0),
            performer=getattr(audio, "performer", None),
            title=getattr(audio, "title", None),
        )
    if info["kind"] == "video":
        video = source.video or source.document
        return InputMediaVideo(
            out_path,
            caption=caption,
            caption_entities=list(source.caption_entities or []),
            parse_mode=None,
            width=int(getattr(video, "width", 0) or 0),
            height=int(getattr(video, "height", 0) or 0),
            duration=int(getattr(video, "duration", 0) or 0),
            supports_streaming=True,
        )
    return InputMediaDocument(
        out_path,
        caption=caption,
        caption_entities=list(source.caption_entities or []),
        parse_mode=None,
    )


async def update_index(source_chat_id, edited):
    info = media_info(edited)
    if not info:
        raise RuntimeError("Edited Telegram message has no supported media")
    row = {
        "storage_chat_id": int(source_chat_id),
        "telegram_message_id": int(edited.id),
        "media_kind": info["kind"],
        "file_id": str(info["file_id"]),
        "file_unique_id": str(info.get("file_unique_id") or ""),
        "file_name": info["name"],
        "mime_type": info["mime"],
        "file_size": int(info["size"]),
        "duration": int(info.get("duration") or 0),
        "width": int(info.get("width") or 0),
        "height": int(info.get("height") or 0),
        "updated_at": now_iso(),
    }
    import asyncio
    await asyncio.to_thread(
        lambda: sb.table("telegram_media_index").upsert(
            row, on_conflict="storage_chat_id,telegram_message_id,media_kind"
        ).execute()
    )


def caption_signature(message):
    entities = []
    for entity in getattr(message, "caption_entities", None) or []:
        entities.append((
            getattr(entity, "type", None),
            getattr(entity, "offset", None),
            getattr(entity, "length", None),
            getattr(entity, "url", None),
            getattr(entity, "language", None),
            getattr(entity, "custom_emoji_id", None),
        ))
    return (message.caption or "", tuple(entities))


async def process_job(job):
    job_id = int(job["id"])
    set_job(
        job_id,
        status="running",
        started_at=job.get("started_at") or now_iso(),
        completed_at=None,
        last_error="",
    )
    ids = []
    for value in (job.get("source_message_ids") or []):
        try:
            mid = int(value)
        except (TypeError, ValueError):
            continue
        if mid > 0 and mid not in ids:
            ids.append(mid)

    target = target_bytes(job)
    successes = int(job.get("success_count") or 0)
    skipped = int(job.get("skipped_count") or 0)
    failures = int(job.get("failed_count") or 0)
    errors = []

    if not ids:
        reason = "Compression job contains no valid Telegram message IDs"
        set_job(
            job_id,
            status="failed",
            total_count=0,
            failed_count=1,
            last_error=reason,
            completed_at=now_iso(),
        )
        await update_owner_status(
            job,
            f"❌ HJ Compression Job #{job_id} failed\n\n{reason}",
        )
        print(f"[JOB {job_id}] FAILED: {reason}", flush=True)
        return False

    for idx, message_id in enumerate(ids, start=1):
        try:
            set_job(
                job_id,
                current_index=idx - 1,
                current_message_id=message_id,
                current_original_size=None,
                current_output_size=None,
                updated_at=now_iso(),
            )
            await update_owner_status(
                job,
                f"🗜️ HJ Compression Job #{job_id}\n\n"
                f"Processing {idx}/{len(ids)}\nMessage: {message_id}\n"
                f"Target: {float(job.get('target_mb') or 19):g} MB\n\n"
                "Fetching Telegram media…",
            )

            source = await _call_with_floodwait(
                lambda: app.get_messages(int(job["source_chat_id"]), int(message_id)),
                f"get message {message_id}",
            )
            if not source:
                raise RuntimeError(f"Message {message_id} was not found")

            info = media_info(source)
            if not info:
                raise RuntimeError(f"Message {message_id} has no supported media")

            if info["size"] <= target:
                await update_index(int(job["source_chat_id"]), source)
                skipped += 1
                set_job(
                    job_id,
                    current_index=idx,
                    skipped_count=skipped,
                    failed_count=failures,
                    current_original_size=info["size"],
                    current_output_size=info["size"],
                    last_error="",
                )
                continue

            if info["size"] > MAX_FILE_BYTES:
                raise RuntimeError(
                    f"Input is {human_size(info['size'])}; this runner intentionally caps one file at 500 MB."
                )

            set_job(
                job_id,
                current_original_size=info["size"],
                current_output_size=None,
                last_error="",
            )
            with tempfile.TemporaryDirectory(prefix=f"hjcmp-{job_id}-") as tmp:
                src_path = str(Path(tmp) / info["name"])
                out_path = str(Path(tmp) / make_output_name(info))

                await update_owner_status(
                    job,
                    f"🗜️ HJ Compression Job #{job_id}\n\n"
                    f"Processing {idx}/{len(ids)}\nMessage: {message_id}\n"
                    f"Original: {human_size(info['size'])}\n"
                    f"Target: {float(job.get('target_mb') or 19):g} MB\n\n"
                    "Downloading…",
                )
                downloaded = await _call_with_floodwait(
                    lambda: app.download_media(source, file_name=src_path),
                    f"download message {message_id}",
                )
                if not downloaded or not Path(downloaded).exists():
                    raise RuntimeError("Telegram media download returned no file")
                src_path = str(downloaded)

                await update_owner_status(
                    job,
                    f"🗜️ HJ Compression Job #{job_id}\n\n"
                    f"Processing {idx}/{len(ids)}\nMessage: {message_id}\n"
                    f"Original: {human_size(info['size'])}\n\nCompressing…",
                )
                if info["kind"] == "audio":
                    output_size = compress_audio(src_path, out_path, target, info["duration"])
                elif info["kind"] == "video":
                    output_size = compress_video(
                        src_path, out_path, target, info["duration"], info["width"], info["height"]
                    )
                else:
                    output_size = optimize_document(src_path, out_path, info["name"])

                if not Path(out_path).exists():
                    raise RuntimeError("Compression produced no output file")
                if output_size >= info["size"]:
                    raise RuntimeError(
                        f"Compressed file is not smaller ({human_size(output_size)} vs {human_size(info['size'])})"
                    )
                if output_size > target:
                    raise RuntimeError(
                        f"Could not reach target: {human_size(output_size)} > {human_size(target)}"
                    )

                await update_owner_status(
                    job,
                    f"🗜️ HJ Compression Job #{job_id}\n\n"
                    f"Processing {idx}/{len(ids)}\nMessage: {message_id}\n"
                    f"Original: {human_size(info['size'])}\n"
                    f"Compressed: {human_size(output_size)}\n\nUploading/replacing…",
                )
                media = build_input_media(info, out_path, source)
                edited = await _call_with_floodwait(
                    lambda: app.edit_message_media(
                        int(job["source_chat_id"]),
                        int(message_id),
                        media,
                    ),
                    f"replace message {message_id}",
                )
                if not edited:
                    raise RuntimeError("Telegram edit_message_media returned no result")

                # Telegram's edit result is not sufficient proof. Verify the real
                # storage message and, if verification fails, roll back while the
                # original downloaded file is still inside the temporary directory.
                try:
                    verified = await _call_with_floodwait(
                        lambda: app.get_messages(
                            int(job["source_chat_id"]), int(message_id)
                        ),
                        f"verify replaced message {message_id}",
                    )
                    if not verified:
                        raise RuntimeError("Post-replacement Telegram verification returned no message")
                    if int(verified.id) != int(message_id):
                        raise RuntimeError(
                            f"Telegram replacement changed message identity: expected {message_id}, got {verified.id}"
                        )
                    verified_info = media_info(verified)
                    if not verified_info:
                        raise RuntimeError("Verified Telegram message has no supported media")
                    if verified_info["kind"] != info["kind"]:
                        raise RuntimeError(
                            f"Telegram replacement changed media kind: {info['kind']} -> {verified_info['kind']}"
                        )
                    if str(verified_info["file_id"]) == str(info["file_id"]):
                        raise RuntimeError("Telegram file_id did not change after replacement")
                    if int(verified_info["size"]) >= int(info["size"]):
                        raise RuntimeError("Telegram replacement is not smaller than the original media")
                    if int(verified_info["size"]) > int(target):
                        raise RuntimeError("Telegram replacement exceeds the requested target size")
                    if caption_signature(verified) != caption_signature(source):
                        raise RuntimeError("Telegram replacement changed the caption or caption entities")
                except Exception as verify_exc:
                    rollback_ok = False
                    try:
                        restored = await _call_with_floodwait(
                            lambda: app.edit_message_media(
                                int(job["source_chat_id"]),
                                int(message_id),
                                build_input_media(info, src_path, source),
                            ),
                            f"rollback message {message_id}",
                        )
                        restored_check = await _call_with_floodwait(
                            lambda: app.get_messages(
                                int(job["source_chat_id"]), int(message_id)
                            ),
                            f"verify rollback {message_id}",
                        )
                        rollback_ok = bool(
                            restored
                            and restored_check
                            and int(restored_check.id) == int(message_id)
                            and caption_signature(restored_check) == caption_signature(source)
                        )
                    except Exception as rollback_exc:
                        print(
                            f"[JOB {job_id}] Rollback failed for message {message_id}: {rollback_exc}",
                            flush=True,
                        )
                    if rollback_ok:
                        raise RuntimeError(
                            f"Post-replacement verification failed; original media was restored: {verify_exc}"
                        ) from verify_exc
                    raise RuntimeError(
                        f"Post-replacement verification failed and rollback could not be verified: {verify_exc}"
                    ) from verify_exc

                await update_index(int(job["source_chat_id"]), verified)
                successes += 1
                set_job(
                    job_id,
                    current_index=idx,
                    success_count=successes,
                    skipped_count=skipped,
                    failed_count=failures,
                    current_output_size=int(verified_info["size"]),
                    current_message_id=message_id,
                    last_error="",
                )
        except Exception as exc:
            failures += 1
            reason = str(exc)[:1000]
            errors.append(f"Message {message_id}: {reason}")
            set_job(
                job_id,
                current_index=idx,
                success_count=successes,
                skipped_count=skipped,
                failed_count=failures,
                current_message_id=message_id,
                last_error=reason,
            )
            await update_owner_status(
                {**job, "current_message_id": message_id},
                f"❌ HJ Compression Job #{job_id} item failed\n\n"
                f"Processing: {idx}/{len(ids)}\nMessage: {message_id}\n"
                f"Error: {reason[:700]}\n\n"
                "Continuing with other independent items…",
            )
            print(f"[JOB {job_id}] Message {message_id} FAILED: {reason}", flush=True)
            continue

    final_error = " | ".join(errors[-3:])[:1000] if errors else ""
    final_status = "failed" if failures else "completed"
    final_message = (
        f"{'❌' if failures else '✅'} HJ Compression Job #{job_id} {final_status}\n\n"
        f"Files: {len(ids)}\n✅ Compressed: {successes}\n"
        f"⏭️ Skipped: {skipped}\n❌ Failed: {failures}"
    )
    if final_error:
        final_message += f"\n\nLast errors:\n{final_error[:1600]}"

    set_job(
        job_id,
        status=final_status,
        current_index=len(ids),
        total_count=len(ids),
        success_count=successes,
        skipped_count=skipped,
        failed_count=failures,
        completed_at=now_iso(),
        last_error=final_error,
    )
    await update_owner_status(job, final_message)
    return final_status == "completed"


async def main():
    init_runtime()
    max_jobs = int(os.environ.get("MAX_COMPRESSION_JOBS", DEFAULT_MAX_JOBS))
    jobs = fetch_jobs(max_jobs)
    if not jobs:
        print("[COMPRESSION] No pending jobs.", flush=True)
        return
    async with app:
        for job in jobs:
            ok = await process_job(job)
            if not ok:
                # Continue with other independent jobs.
                continue


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
