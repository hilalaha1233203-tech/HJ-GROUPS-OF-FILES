"""HJ GROUPS Store Keeper compression controls.

The Telegram bot only creates durable jobs and displays status. Heavy media work
runs on an ephemeral GitHub Actions runner so Render never handles large media
egress or FFmpeg work.
"""

import re
import uuid
from datetime import datetime, timezone

from pyrogram import StopPropagation, filters
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import bot_legacy
from configs import Config
from handlers.database import db


SESSION = {}
MAX_BULK_MESSAGES = 5000
JOB_CHUNK_SIZE = 1
SUPPORTED_KINDS = ("audio", "video", "document")


def _owner(user_id: int) -> bool:
    return int(user_id) == int(Config.BOT_OWNER)


def _fmt_size(value):
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "Unknown"
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.2f} MB"
    return f"{size / 1024**3:.2f} GB"


def _safe_kind(message: Message):
    if not message:
        return None
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "video", None):
        return "video"
    document = getattr(message, "document", None)
    if document:
        mime = str(getattr(document, "mime_type", "") or "").lower()
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
        return "document"
    return None


def _target_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 19 MB — Web Stream", callback_data="cmp:target:19"),
            InlineKeyboardButton("⚖️ 30 MB — Balanced", callback_data="cmp:target:30"),
        ],
        [
            InlineKeyboardButton("✨ 45 MB — High Quality", callback_data="cmp:target:45"),
            InlineKeyboardButton("✏️ Custom", callback_data="cmp:custom"),
        ],
        [InlineKeyboardButton("✖️ Cancel", callback_data="cmp:center")],
    ])


def _center_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎧 Audio", callback_data="cmp:audio"),
            InlineKeyboardButton("🎬 Video", callback_data="cmp:video"),
        ],
        [
            InlineKeyboardButton("📄 Document", callback_data="cmp:document"),
            InlineKeyboardButton("📦 Bulk Compression", callback_data="cmp:bulk"),
        ],
        [
            InlineKeyboardButton("📊 Compression Jobs", callback_data="cmp:jobs"),
            InlineKeyboardButton("🌐 Streaming Index", callback_data="cmp:stream"),
        ],
        [InlineKeyboardButton("✖️ Close", callback_data="closeMessage")],
    ])


async def show_compression_center(message: Message):
    await message.edit_text(
        "**🗜️ HJ GROUPS — COMPRESSION CENTER**\n\n"
        "Heavy compression runs on an ephemeral GitHub Actions runner.\n\n"
        "• 🎧 Audio — adaptive AAC/M4A\n"
        "• 🎬 Video — H.264/AAC streaming-friendly MP4\n"
        "• 📄 Documents — PDF/ZIP-family optimisation when safe\n"
        "• 📦 Bulk — small resumable jobs\n"
        "• 🌐 19 MB is the website-streaming profile\n\n"
        "Files above the Telegram Bot API 20 MB download limit are not indexed "
        "for website streaming until they are compressed.",
        reply_markup=_center_keyboard(),
    )


async def _storage_channels():
    channels = await db.get_storage_channels()
    if channels:
        return [int(x) for x in channels]
    fallback = await db.get_db_channel_id()
    return [int(fallback)] if fallback is not None else []


async def _default_storage_channel():
    channels = await _storage_channels()
    if not channels:
        raise RuntimeError(
            "Storage channel is not configured. Forward one storage-channel message to the bot first."
        )
    return channels[0]


def _extract_origin(message: Message):
    try:
        return bot_legacy._forwarded_origin(message)
    except Exception:
        return None


async def _resolve_single_source(bot, command: Message):
    if command.reply_to_message:
        origin = _extract_origin(command.reply_to_message)
        if origin:
            chat_id, message_id = int(origin[0]), int(origin[1])
            if chat_id not in await _storage_channels():
                raise ValueError("Reply to a message from an HJ storage channel.")
            return chat_id, message_id

    args = list(command.command[1:])
    if len(args) == 1 and args[0].isdigit():
        return await _default_storage_channel(), int(args[0])

    if len(args) == 2:
        first, second = args
        try:
            chat_id = int(first)
        except ValueError:
            chat = await bot.get_chat(first)
            chat_id = int(chat.id)
        message_id = int(second)
        if chat_id not in await _storage_channels():
            raise ValueError("Only configured HJ storage channels can be compressed.")
        return chat_id, message_id

    raise ValueError(
        "Use /compress as a reply to a storage file, or /compress <message_id>."
    )


async def _get_source_message(bot, chat_id, message_id):
    await bot.get_chat(int(chat_id))
    message = await bot.get_messages(int(chat_id), int(message_id))
    if not message:
        raise ValueError(f"Telegram message {message_id} was not found.")
    kind = _safe_kind(message)
    if kind not in SUPPORTED_KINDS:
        raise ValueError(
            f"Message {message_id} is not a supported audio, video or document."
        )
    return message, kind


async def _scan_range(bot, chat_id, start_id, end_id, kind, target_bytes):
    low, high = sorted((int(start_id), int(end_id)))
    if high - low > MAX_BULK_MESSAGES:
        raise ValueError(f"Bulk range is limited to {MAX_BULK_MESSAGES + 1} message IDs.")

    await bot.get_chat(int(chat_id))
    result = []
    async for message in bot.get_chat_history(
        int(chat_id), limit=MAX_BULK_MESSAGES + 1
    ):
        if message is None:
            continue
        mid = int(message.id)
        if mid < low:
            break
        if mid > high:
            continue
        if _safe_kind(message) != kind:
            continue
        media = (
            getattr(message, "audio", None)
            or getattr(message, "video", None)
            or getattr(message, "document", None)
        )
        size = int(getattr(media, "file_size", 0) or 0)
        if size > int(target_bytes):
            result.append(mid)
    return sorted(set(result))


async def _supabase_insert(row):
    response = await db._execute(
        lambda: db.client.table("compression_jobs").insert(row).execute(),
        "create compression job",
    )
    data = response.data or []
    if not data:
        raise RuntimeError("Compression job was not created.")
    return data[0]


async def _queue_jobs(*, status_message, requested_by, source_chat_id, message_ids, media_kind, target_mb):
    ids = []
    seen = set()
    for value in message_ids:
        try:
            mid = int(value)
        except (TypeError, ValueError):
            continue
        if mid > 0 and mid not in seen:
            ids.append(mid)
            seen.add(mid)

    if not ids:
        raise ValueError("No matching media files were found above the selected target size.")

    group_key = uuid.uuid4().hex
    chunks = [ids[i:i + JOB_CHUNK_SIZE] for i in range(0, len(ids), JOB_CHUNK_SIZE)]
    batch_total = len(chunks)
    status_message_id = int(status_message.id)
    created = 0
    now = datetime.now(timezone.utc).isoformat()

    await status_message.edit_text(
        "⏳ Preparing compression queue...\n\n"
        f"Media type: {media_kind}\n"
        f"Files selected: {len(ids)}\n"
        f"Target: {target_mb:g} MB\n"
        f"Jobs: {batch_total}\n\n"
        "Each job is independent; one failure does not stop the other jobs."
    )

    try:
        for index, chunk in enumerate(chunks, start=1):
            await _supabase_insert({
                "requested_by": int(requested_by),
                "requested_chat_id": int(requested_by),
                "status_message_id": status_message_id,
                "source_chat_id": int(source_chat_id),
                "source_message_ids": chunk,
                "media_kind": media_kind,
                "target_mb": float(target_mb),
                "profile": "web-stream" if float(target_mb) <= 19 else "balanced",
                "keep_original": False,
                "status": "pending",
                "current_index": 0,
                "total_count": len(chunk),
                "success_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "current_message_id": None,
                "current_original_size": None,
                "current_output_size": None,
                "last_error": "",
                "group_key": group_key,
                "batch_index": index,
                "batch_total": batch_total,
                "details": {},
                "created_at": now,
                "updated_at": now,
            })
            created += 1
    except Exception as exc:
        await status_message.edit_text(
            "⚠️ Compression queue creation was interrupted.\n\n"
            f"Created jobs: {created}/{batch_total}\n"
            f"Error: {str(exc)[:300]}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Jobs", callback_data="cmp:jobs")],
                [InlineKeyboardButton("⬅️ Back", callback_data="cmp:center")],
            ]),
        )
        raise

    await status_message.edit_text(
        "✅ Compression queued successfully\n\n"
        f"Media: {media_kind}\n"
        f"Files: {len(ids)}\n"
        f"Target: {target_mb:g} MB\n"
        f"Jobs created: {created}\n\n"
        "The ephemeral GitHub runner checks the queue automatically.\n"
        "Render is not used for large-file compression.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 View Jobs", callback_data="cmp:jobs")],
            [InlineKeyboardButton("🌐 Streaming Index", callback_data="cmp:stream")],
            [InlineKeyboardButton("⬅️ Compression Center", callback_data="cmp:center")],
        ]),
    )
    return created


def _target_text(session):
    ids = session.get("ids") or []
    extra = ""
    if len(ids) == 1 and session.get("single_size"):
        extra = f"\nOriginal size: {_fmt_size(session['single_size'])}"
    return (
        f"🗜️ {session.get('kind', '').title()} Compression\n\n"
        f"Files selected: {len(ids)}{extra}\n\n"
        "Choose a target size. Use 19 MB for website streaming."
    )

async def _send_target_prompt(message, session):
    prompt = await message.reply_text(
        _target_text(session),
        reply_markup=_target_keyboard(),
    )
    session["ui_message_id"] = int(prompt.id)
    return prompt

async def _get_ui_message(bot, user_id, session):
    ui_id = session.get("ui_message_id")
    if not ui_id:
        return None
    try:
        return await bot.get_messages(int(user_id), int(ui_id))
    except Exception:
        return None

async def _queue_session(bot, query, target_mb):
    user_id = int(query.from_user.id)
    session = SESSION.get(user_id)
    if not session or session.get("step") != "target":
        await query.answer("Compression session expired. Run /compression again.", show_alert=True)
        return

    try:
        if session.get("single"):
            ids = session["ids"]
        else:
            target_bytes = int(float(target_mb) * 1024 * 1024)
            ids = await _scan_range(
                bot,
                int(session["chat_id"]),
                int(session["start_id"]),
                int(session["end_id"]),
                session["kind"],
                target_bytes,
            )

        status_message = query.message
        if status_message is None:
            status_message = await _get_ui_message(bot, user_id, session)
        if status_message is None:
            raise RuntimeError("Compression status message could not be recovered.")

        await _queue_jobs(
            status_message=status_message,
            requested_by=user_id,
            source_chat_id=int(session["chat_id"]),
            message_ids=ids,
            media_kind=session["kind"],
            target_mb=float(target_mb),
        )
        SESSION.pop(user_id, None)
        try:
            await query.answer("Compression queued.")
        except Exception:
            pass
    except Exception as exc:
        status_message = query.message or await _get_ui_message(bot, user_id, session)
        if status_message:
            await status_message.edit_text(
                "❌ Compression queue failed\n\n"
                f"{str(exc)[:500]}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Try Again", callback_data="cmp:center")],
                ]),
            )
        try:
            await query.answer("Compression queue failed.", show_alert=True)
        except Exception:
            pass

async def _single_start(bot, command):
    chat_id, message_id = await _resolve_single_source(bot, command)
    message, kind = await _get_source_message(bot, chat_id, message_id)
    session = {
        "step": "target",
        "single": True,
        "kind": kind,
        "chat_id": int(chat_id),
        "ids": [int(message_id)],
        "single_size": int(
            getattr(
                getattr(message, "audio", None)
                or getattr(message, "video", None)
                or getattr(message, "document", None),
                "file_size",
                0,
            )
            or 0
        ),
    }
    SESSION[int(command.from_user.id)] = session
    await _send_target_prompt(command, session)


async def _set_bulk_session(user_id, kind, raw_range):
    channels = await _storage_channels()
    if not channels:
        raise RuntimeError("Storage channel is not configured.")
    value = str(raw_range or "").strip()
    match = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", value)
    if match:
        start_id, end_id = int(match.group(1)), int(match.group(2))
    elif value.lower() == "all":
        start_id, end_id = 1, 2**31 - 1
    else:
        raise ValueError("Send a range like 21-1114 or all.")

    SESSION[int(user_id)] = {
        "step": "target",
        "single": False,
        "kind": kind,
        "chat_id": int(channels[0]),
        "start_id": start_id,
        "end_id": end_id,
        "ids": [],
    }


@Bot.on_message(filters.private & filters.command("compression"), group=-5)
async def compression_command(_, message):
    if not _owner(message.from_user.id):
        await message.reply_text("⛔ Owner/Admin Only")
        raise StopPropagation
    await message.reply_text(
        "🗜️ HJ GROUPS — COMPRESSION CENTER",
        reply_markup=_center_keyboard(),
    )
    raise StopPropagation


@Bot.on_message(filters.private & filters.command("compress"), group=-5)
async def compress_command(bot, message):
    if not _owner(message.from_user.id):
        await message.reply_text("⛔ Owner/Admin Only")
        raise StopPropagation
    try:
        await _single_start(bot, message)
    except Exception as exc:
        await message.reply_text(f"❌ {str(exc)[:500]}")
    raise StopPropagation


@Bot.on_message(filters.private & filters.command("compress_bulk"), group=-5)
async def compress_bulk_command(bot, message):
    if not _owner(message.from_user.id):
        await message.reply_text("⛔ Owner/Admin Only")
        raise StopPropagation

    args = list(message.command[1:])
    if len(args) >= 2:
        kind = args[0].lower()
        if kind not in SUPPORTED_KINDS:
            await message.reply_text("Use audio, video or document.")
            raise StopPropagation
        try:
            await _set_bulk_session(message.from_user.id, kind, " ".join(args[1:]))
            await _send_target_prompt(message, SESSION[int(message.from_user.id)])
        except Exception as exc:
            await message.reply_text(f"❌ {str(exc)[:500]}")
        raise StopPropagation

    await message.reply_text(
        "📦 Bulk Compression\n\n"
        "Use /compress_bulk audio 21-1114\n"
        "or /compress_bulk document all.",
        reply_markup=_center_keyboard(),
    )
    raise StopPropagation


@Bot.on_message(filters.private & filters.command("compression_status"), group=-5)
async def compression_status_command(_, message):
    if not _owner(message.from_user.id):
        await message.reply_text("⛔ Owner/Admin Only")
        raise StopPropagation
    await _show_jobs(message, int(message.from_user.id))
    raise StopPropagation


@Bot.on_message(filters.private & filters.command("compression_cancel"), group=-5)
async def compression_cancel_command(_, message):
    if not _owner(message.from_user.id):
        await message.reply_text("⛔ Owner/Admin Only")
        raise StopPropagation
    args = list(message.command[1:])
    if not args or not args[0].isdigit():
        await message.reply_text("Use /compression_cancel <job_id>.")
        raise StopPropagation
    try:
        result = await db._execute(
            lambda: db.client.table("compression_jobs")
            .update({
                "status": "cancelled",
                "last_error": "Cancelled by owner",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", int(args[0]))
            .eq("requested_by", int(message.from_user.id))
            .eq("status", "pending")
            .execute(),
            "cancel compression job",
        )
        await message.reply_text(
            f"✅ Job {args[0]} cancelled."
            if result.data
            else "⚠️ Job not found, already running, or already finished."
        )
    except Exception as exc:
        await message.reply_text(f"❌ {str(exc)[:400]}")
    raise StopPropagation


async def _show_jobs(message, user_id):
    response = await db._execute(
        lambda: db.client.table("compression_jobs")
        .select(
            "id,media_kind,target_mb,status,current_index,total_count,"
            "success_count,skipped_count,failed_count,current_message_id,"
            "last_error,group_key,batch_index,batch_total,created_at"
        )
        .eq("requested_by", int(user_id))
        .order("created_at", desc=True)
        .limit(12)
        .execute(),
        "list compression jobs",
    )
    rows = response.data or []
    if not rows:
        await message.reply_text(
            "📊 Compression Jobs\n\nNo compression jobs yet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗜️ Compression Center", callback_data="cmp:center")],
            ]),
        )
        return

    lines = ["📊 HJ GROUPS — COMPRESSION JOBS", ""]
    buttons = []
    for row in rows:
        status = str(row.get("status") or "unknown").upper()
        target = float(row.get("target_mb") or 0)
        progress = f"{row.get('current_index', 0)}/{row.get('total_count', 0)}"
        lines.append(
            f"#{row.get('id')} · {row.get('media_kind')} · {target:g} MB\n"
            f"Status: {status} · Progress: {progress} · "
            f"OK {row.get('success_count', 0)} · Skip {row.get('skipped_count', 0)} · "
            f"Fail {row.get('failed_count', 0)}"
        )
        if status == "PENDING":
            buttons.append([InlineKeyboardButton(
                f"❌ Cancel #{row.get('id')}",
                callback_data=f"cmp:cancel:{row.get('id')}",
            )])
        if row.get("last_error") and status == "FAILED":
            lines.append(f"Error: {str(row['last_error'])[:180]}")
        lines.append("")

    buttons.extend([
        [InlineKeyboardButton("🔄 Refresh", callback_data="cmp:jobs")],
        [InlineKeyboardButton("⬅️ Back", callback_data="cmp:center")],
    ])
    await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _show_stream_status(message):
    try:
        count_response = await db._execute(
            lambda: db.client.table("telegram_media_index")
            .select("telegram_message_id", count="exact", head=True)
            .execute(),
            "count streaming index",
        )
        latest_response = await db._execute(
            lambda: db.client.table("telegram_media_index")
            .select("updated_at,telegram_message_id,media_kind")
            .order("updated_at", desc=True)
            .limit(1)
            .execute(),
            "latest streaming index",
        )
        total = int(getattr(count_response, "count", 0) or 0)
        latest = (latest_response.data or [None])[0]
        await message.reply_text(
            "🌐 HJ WEBSITE STREAMING INDEX\n\n"
            f"Indexed media records: {total}\n"
            f"Latest message: {latest.get('telegram_message_id') if latest else '—'}\n"
            f"Latest type: {latest.get('media_kind') if latest else '—'}\n"
            f"Updated: {latest.get('updated_at') if latest else '—'}\n\n"
            "The index is refreshed automatically by GitHub Actions.\n"
            "Website Bot API streaming is intended for files at or below 20 MB.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="cmp:stream")],
                [InlineKeyboardButton("⬅️ Compression Center", callback_data="cmp:center")],
            ]),
        )
    except Exception as exc:
        await message.reply_text(
            "⚠️ Streaming index is not ready yet.\n\n"
            f"{str(exc)[:400]}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Compression Center", callback_data="cmp:center")],
            ]),
        )


@Bot.on_callback_query(filters.regex(r"^cmp:bulk:(audio|video|document)$"), group=-6)
async def compression_bulk_type(_, query):
    if not _owner(query.from_user.id):
        await query.answer("Owner/Admin Only", show_alert=True)
        raise StopPropagation
    kind = str(query.data).split(":")[-1]
    SESSION[int(query.from_user.id)] = {
        "step": "bulk_range",
        "single": False,
        "kind": kind,
    }
    await query.message.edit_text(
        f"📦 Bulk {kind.title()} Compression\n\n"
        "Send a message-ID range from the configured storage channel.\n\n"
        "Examples:\n"
        "21-1114\n"
        "100-250\n"
        "all\n\n"
        "Only matching media above the final target will be queued.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Cancel", callback_data="cmp:center")],
        ]),
    )
    await query.answer()
    raise StopPropagation


@Bot.on_callback_query(filters.regex(r"^cmp:"), group=-5)
async def compression_callback(bot, query: CallbackQuery):
    if not _owner(query.from_user.id):
        await query.answer("Owner/Admin Only", show_alert=True)
        raise StopPropagation

    data = str(query.data or "")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    try:
        if action == "center":
            SESSION.pop(int(query.from_user.id), None)
            await show_compression_center(query.message)

        elif action in {"audio", "video", "document"}:
            label = action.title()
            icon = "🎧" if action == "audio" else "🎬" if action == "video" else "📄"
            await query.message.edit_text(
                f"{icon} {label} Compression\n\n"
                "Single file:\n"
                "Reply to the storage message and send /compress\n\n"
                "Bulk:\n"
                "Use the button below to select a message range.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"📦 Bulk {label}", callback_data=f"cmp:bulk:{action}")],
                    [InlineKeyboardButton("🗜️ Single-file Help", callback_data="cmp:reply_help")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="cmp:center")],
                ]),
            )
            await query.answer()

        elif action == "reply_help":
            await query.message.edit_text(
                "🗜️ Single File Compression\n\n"
                "Reply to a message from an HJ storage channel and send /compress.\n\n"
                "Or use /compress <message_id>.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Back", callback_data="cmp:center")],
                ]),
            )
            await query.answer()

        elif action == "bulk":
            await query.message.edit_text(
                "📦 Bulk Compression\n\nSelect the media type.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎧 Audio", callback_data="cmp:bulk:audio")],
                    [InlineKeyboardButton("🎬 Video", callback_data="cmp:bulk:video")],
                    [InlineKeyboardButton("📄 Document", callback_data="cmp:bulk:document")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="cmp:center")],
                ]),
            )
            await query.answer()

        elif action == "target":
            target = float(parts[2])
            if not 1 <= target <= 45:
                raise ValueError("Target size must be between 1 and 45 MB.")
            await _queue_session(bot, query, target)

        elif action == "custom":
            session = SESSION.get(int(query.from_user.id))
            if not session:
                raise ValueError("Compression session expired.")
            session["step"] = "custom_target"
            await query.message.edit_text(
                "✏️ Custom Target Size\n\n"
                "Send a number between 1 and 45 MB.\n"
                "Example: 18.5",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Cancel", callback_data="cmp:center")],
                ]),
            )
            await query.answer()

        elif action == "jobs":
            await _show_jobs(query.message, int(query.from_user.id))
            await query.answer()

        elif action == "stream":
            await _show_stream_status(query.message)
            await query.answer()

        elif action == "cancel" and len(parts) >= 3:
            job_id = int(parts[2])
            result = await db._execute(
                lambda: db.client.table("compression_jobs")
                .update({
                    "status": "cancelled",
                    "last_error": "Cancelled by owner",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                .eq("id", job_id)
                .eq("requested_by", int(query.from_user.id))
                .eq("status", "pending")
                .execute(),
                "cancel compression job",
            )
            if result.data:
                await query.answer(f"Job {job_id} cancelled.")
            else:
                await query.answer("Job is not pending or was not found.", show_alert=True)
            await _show_jobs(query.message, int(query.from_user.id))
            return

        else:
            await query.answer()
    except Exception as exc:
        await query.answer(str(exc)[:180], show_alert=True)

    raise StopPropagation


@Bot.on_message(filters.private & filters.text, group=-4)
async def compression_text_input(bot, message):
    user_id = int(message.from_user.id)
    if not _owner(user_id):
        return
    session = SESSION.get(user_id)
    if not session:
        return

    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        SESSION.pop(user_id, None)
        await message.reply_text("✅ Compression session cancelled.")
        raise StopPropagation

    if session.get("step") == "custom_target":
        try:
            target = float(value)
        except ValueError:
            await message.reply_text("❌ Send a number between 1 and 45 MB.")
            raise StopPropagation
        if not 1 <= target <= 45:
            await message.reply_text("❌ Target must be between 1 and 45 MB.")
            raise StopPropagation
        status_message = await message.reply_text("⏳ Starting compression queue...")
        session["ui_message_id"] = int(status_message.id)
        query = type(
            "SyntheticQuery",
            (),
            {
                "message": status_message,
                "from_user": message.from_user,
                "answer": lambda *args, **kwargs: None,
            },
        )()
        await _queue_session(bot, query, target)
        raise StopPropagation

    if session.get("step") == "bulk_range":
        match = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)|all", value, flags=re.I)
        if not match:
            await message.reply_text("❌ Use a range like 21-1114 or all.")
            raise StopPropagation
        if value.lower() == "all":
            start_id, end_id = 1, 2**31 - 1
        else:
            start_id, end_id = int(match.group(1)), int(match.group(2))
        session.update(
            step="target",
            start_id=start_id,
            end_id=end_id,
            single=False,
            ids=[],
        )
        await _send_target_prompt(message, session)
        raise StopPropagation
