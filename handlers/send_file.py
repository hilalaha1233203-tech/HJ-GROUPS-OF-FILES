# HJ GROUPS OF FILES - File delivery

import asyncio
import inspect
from pyrogram import Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from configs import Config
from handlers.database import db
from handlers.telegram_api import copy_message as api_copy_message, edit_message_caption as api_edit_message_caption, edit_message_reply_markup as api_edit_message_reply_markup

_BATCH_DELETE_CONTEXTS = {}

# Telegram documents a practical free broadcast ceiling of about 30 messages/sec.
# Keep media delivery below that ceiling so many simultaneous /start requests
# queue locally instead of creating a burst of 429 responses.
_DELIVERY_RATE_LIMIT = 25
_DELIVERY_RATE_WINDOW = 1.0
_DELIVERY_RATE_LOCK = asyncio.Lock()
_DELIVERY_TIMESTAMPS = []
_DELIVERY_USER_TIMESTAMPS = {}


async def _acquire_delivery_slot(user_id=None):
    while True:
        async with _DELIVERY_RATE_LOCK:
            now = asyncio.get_running_loop().time()
            cutoff = now - _DELIVERY_RATE_WINDOW
            while _DELIVERY_TIMESTAMPS and _DELIVERY_TIMESTAMPS[0] <= cutoff:
                _DELIVERY_TIMESTAMPS.pop(0)
            user_wait = 0.0
            if user_id is not None:
                last_user = _DELIVERY_USER_TIMESTAMPS.get(int(user_id), 0.0)
                user_wait = max(0.0, 1.0 - (now - last_user))

            global_wait = 0.0
            if len(_DELIVERY_TIMESTAMPS) >= _DELIVERY_RATE_LIMIT:
                global_wait = max(0.01, _DELIVERY_TIMESTAMPS[0] + _DELIVERY_RATE_WINDOW - now)

            wait_for = max(user_wait, global_wait)
            if wait_for <= 0:
                _DELIVERY_TIMESTAMPS.append(now)
                if user_id is not None:
                    _DELIVERY_USER_TIMESTAMPS[int(user_id)] = now
                return
        await asyncio.sleep(wait_for)


def human_size(size):
    try:
        size = int(size)
    except Exception:
        return "Unknown"
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.2f} KB"
    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.2f} MB"
    return f"{size / (1024 ** 3):.2f} GB"


def _file_meta(message: Message):
    if message is None:
        return "Telegram Media", None
    media = (
        getattr(message, "document", None)
        or getattr(message, "audio", None)
        or getattr(message, "video", None)
        or getattr(message, "animation", None)
    )
    if media is not None:
        return getattr(media, "file_name", None) or "Telegram Media", getattr(media, "file_size", None)
    if getattr(message, "photo", None):
        return "Photo.jpg", getattr(message.photo, "file_size", None)
    if getattr(message, "voice", None):
        return "Voice Message.ogg", getattr(message.voice, "file_size", None)
    return "Telegram Message", None


def build_caption(message: Message):
    name, size = _file_meta(message)
    return f"File Name : {name}\n\nFile size : {human_size(size)}\n\n{Config.DELIVERY_TAG}"


def _url(value):
    value = str(value or "").strip()
    if not value:
        return None
    if value.startswith("https://") or value.startswith("http://"):
        return value
    if value.startswith("@"):
        return f"https://t.me/{value[1:]}"
    return None


def build_channel_buttons():
    rows = []
    for label, value in (
        ("Main Channel", Config.MAIN_CHANNEL),
        ("Backup Channel", Config.BACKUP_CHANNEL),
        ("Pocket Library", Config.POCKET_LIBRARY),
    ):
        url = _url(value)
        if url:
            rows.append([InlineKeyboardButton(label, url=url)])
    return InlineKeyboardMarkup(rows) if rows else None


def build_channel_buttons_json():
    rows = []
    for label, value in (
        ("Main Channel", Config.MAIN_CHANNEL),
        ("Backup Channel", Config.BACKUP_CHANNEL),
        ("Pocket Library", Config.POCKET_LIBRARY),
    ):
        url = _url(value)
        if url:
            rows.append([{"text": label, "url": url}])
    return {"inline_keyboard": rows} if rows else None


def format_delete_time(seconds):
    try:
        seconds = int(seconds)
    except Exception:
        return "the selected time"
    if seconds <= 0:
        return None
    if seconds % 3600 == 0:
        value = seconds // 3600
        return f"{value} hour" + ("s" if value != 1 else "")
    if seconds % 60 == 0:
        value = seconds // 60
        return f"{value} minute" + ("s" if value != 1 else "")
    return f"{seconds} second" + ("s" if seconds != 1 else "")


def build_delete_notice(delay):
    human = format_delete_time(delay)
    if not human:
        return None
    return (
        "❗️❗️❗️𝕀𝕄ℙ𝕆ℝ𝕋𝔸ℕ𝕋❗️️❗️❗️\n\n"
        f'ᴛʜɪꜱ ᴍᴇꜱꜱᴀɢᴇ ᴡɪʟʟ ʙᴇ ᴅᴇʟᴇᴛᴇᴅ ɪɴ "{human}" ⏳ (ᴅᴜᴇ ᴛᴏ ᴄᴏᴘʏʀɪɢʜᴛ ɪꜱꜱᴜᴇꜱ)🤕.\n\n'
        f'ᴘʟᴇᴀꜱᴇ ʟɪꜱᴛᴇɴ ᴛʜɪꜱ ᴍᴇꜱꜱᴀɢᴇ ʙᴇꜰᴏʀᴇ "{human}" ᴛᴏ ᴀᴠᴏɪᴅ ʟᴏꜱɪɴɢ...\n\n'
        "𝕋𝕙𝕒𝕟𝕜 𝕐𝕠𝕦 𝔽𝕠𝕣 ℂ𝕙𝕠𝕠𝕤𝕚𝕟𝕘 ℍ𝕁 𝔾𝕣𝕠𝕦𝕡𝕤🔮"
    )


async def _safe_get_source(bot, channel_id, file_id):
    try:
        return await bot.get_messages(chat_id=channel_id, message_ids=file_id)
    except Exception:
        return None


async def media_forward(bot: Client, user_id: int, file_id: int, channel_id=None):
    channel_id = channel_id or await db.get_db_channel_id()
    if channel_id is None:
        raise RuntimeError("Storage channel is not configured.")

    protect_content = await db.get_protect_content()
    source = await _safe_get_source(bot, channel_id, file_id)
    caption = build_caption(source) if source else None
    reply_markup = build_channel_buttons()

    try:
        await _acquire_delivery_slot(user_id)
        return await bot.copy_message(
            chat_id=user_id,
            from_chat_id=channel_id,
            message_id=file_id,
            caption=caption,
            protect_content=protect_content,
            reply_markup=reply_markup,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await media_forward(bot, user_id, file_id, channel_id)
    except Exception as pyrogram_error:
        try:
            await _acquire_delivery_slot(user_id)
            copied = await api_copy_message(
                chat_id=user_id,
                from_chat_id=channel_id,
                message_id=file_id,
                protect_content=protect_content,
            )
            copied_id = (copied or {}).get("message_id")
            if copied_id and caption:
                try:
                    await api_edit_message_caption(user_id, copied_id, caption)
                except Exception:
                    pass
            if copied_id:
                buttons = build_channel_buttons_json()
                if buttons:
                    try:
                        await api_edit_message_reply_markup(user_id, copied_id, buttons)
                    except Exception:
                        pass
            return copied
        except Exception:
            raise pyrogram_error


async def _delete_delivered_messages(bot: Client, chat_id: int, message_ids, delay: int):
    """Compatibility wrapper: persist the deletion instead of a volatile sleep task."""
    return await schedule_persistent_delete(chat_id, message_ids, delay)


async def schedule_persistent_delete(chat_id: int, message_ids, delay: int):
    if int(delay) <= 0:
        return None
    return await db.enqueue_auto_delete(int(chat_id), message_ids, int(delay))


async def _process_auto_delete_job(bot, job):
    from handlers.telegram_api import delete_messages as api_delete_messages
    job_id = int(job["id"])
    if not await db.mark_auto_delete_processing(job_id, int(job.get("attempts") or 0)):
        return False
    try:
        ids = []
        for value in job.get("message_ids") or []:
            try:
                mid = int(value)
            except (TypeError, ValueError):
                continue
            if mid > 0 and mid not in ids:
                ids.append(mid)
        if not ids:
            await db.complete_auto_delete(job_id)
            return True
        for offset in range(0, len(ids), 100):
            chunk = ids[offset:offset + 100]
            try:
                await _acquire_delivery_slot(int(job["chat_id"]))
                await api_delete_messages(int(job["chat_id"]), chunk)
            except Exception as bulk_error:
                # If one stale/already-deleted ID poisons a bulk delete, fall
                # back to individual deletes so valid messages are still removed.
                from handlers.telegram_api import delete_message as api_delete_message
                for mid in chunk:
                    try:
                        await _acquire_delivery_slot(int(job["chat_id"]))
                        await api_delete_message(int(job["chat_id"]), mid)
                    except Exception as one_error:
                        message = str(one_error).lower()
                        if any(token in message for token in (
                            "message to delete not found",
                            "message_id_invalid",
                            "message identifier is not specified",
                        )):
                            continue
                        raise bulk_error
        await db.complete_auto_delete(job_id)
        print(f"[AUTO_DELETE] Completed job={job_id} chat={job['chat_id']} messages={len(ids)}")
        return True
    except FloodWait as exc:
        # Retry the whole durable job instead of completing after only one
        # chunk. Telegram skips IDs that are already gone, so retrying is safe.
        retry_delay = max(60, int(exc.value))
        await db.retry_auto_delete(job_id, exc, retry_delay)
        print(f"[AUTO_DELETE] Job={job_id} FloodWait; retry in {retry_delay}s")
        return False
    except Exception as exc:
        attempts = int(job.get("attempts") or 0)
        retry_delay = min(3600, max(30, 30 * (2 ** min(attempts, 5))))
        await db.retry_auto_delete(job_id, exc, retry_delay)
        print(f"[AUTO_DELETE] Job={job_id} retry in {retry_delay}s: {exc}")
        return False


async def auto_delete_worker(bot: Client):
    """Durable worker: pending deletions survive bot restarts/deployments."""
    print("[AUTO_DELETE] Persistent worker started")
    try:
        await db.recover_stale_auto_deletes(120)
    except Exception as exc:
        print(f"[AUTO_DELETE] Startup recovery failed: {exc}")
    while True:
        try:
            jobs = await db.get_due_auto_deletes(limit=20)
            if jobs:
                for job in jobs:
                    await _process_auto_delete_job(bot, job)
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[AUTO_DELETE] Worker error: {exc}")
            await asyncio.sleep(5)


async def send_delete_notice(bot: Client, user_id: int, delay: int):
    text = build_delete_notice(delay)
    if not text:
        return None
    try:
        await _acquire_delivery_slot(user_id)
        return await bot.send_message(chat_id=user_id, text=text, disable_web_page_preview=True)
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await send_delete_notice(bot, user_id, delay)


def _legacy_batch_context():
    """Detect the legacy /start batch loop without changing bot_legacy.py."""
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame is not None:
            if frame.f_code.co_name == "start" and "message_ids" in frame.f_locals:
                ids = frame.f_locals.get("message_ids")
                current_id = frame.f_locals.get("message_id")
                if isinstance(ids, (list, tuple)) and len(ids) > 1 and current_id is not None:
                    try:
                        normalized_ids = [int(x) for x in ids]
                        return {
                            "ids": normalized_ids,
                            "current_id": int(current_id),
                        }
                    except Exception:
                        return None
            frame = frame.f_back
    finally:
        del frame
    return None


async def _send_batch_delete_notice_once(bot, user_id: int, delivered_ids, delay: int):
    if delay <= 0:
        return
    try:
        notice = await send_delete_notice(bot, user_id, delay)
        if notice is not None:
            notice_id = getattr(notice, "id", None) if not isinstance(notice, dict) else notice.get("message_id")
            if notice_id:
                delivered_ids.append(int(notice_id))
    except Exception as notice_error:
        # Keep the durable delete schedule even if the notice cannot be sent.
        print(f"[AUTO_DELETE] Batch notice failed for user={user_id}: {notice_error}")
    await schedule_persistent_delete(user_id, delivered_ids, delay)


async def send_media_and_reply(
    bot: Client,
    user_id: int,
    file_id: int,
    channel_id=None,
    show_notice=True,
    schedule_delete=True,
):
    sent_message = await media_forward(bot, user_id, file_id, channel_id)
    if sent_message is None:
        return []

    delivered = [m for m in sent_message] if isinstance(sent_message, (list, tuple)) else [sent_message]
    message_ids = [
        getattr(m, "id", None) if not isinstance(m, dict) else m.get("message_id")
        for m in delivered
    ]

    delay = await db.get_auto_delete_seconds()
    batch_context = _legacy_batch_context()
    if batch_context:
        task = asyncio.current_task()
        key = id(task)
        state = _BATCH_DELETE_CONTEXTS.setdefault(
            key,
            {
                "message_ids": [],
                "expected_ids": batch_context["ids"],
                "delay": delay,
            },
        )
        state["message_ids"].extend(int(mid) for mid in message_ids if mid)
        state["delay"] = delay
        if int(file_id) == int(batch_context["ids"][-1]):
            final_ids = list(state["message_ids"])
            _BATCH_DELETE_CONTEXTS.pop(key, None)
            await _send_batch_delete_notice_once(bot, user_id, final_ids, delay)
        return delivered

    if show_notice:
        try:
            notice = await send_delete_notice(bot, user_id, delay)
            if notice is not None:
                notice_id = getattr(notice, "id", None)
                if notice_id:
                    message_ids.append(int(notice_id))
        except Exception as notice_error:
            # A notice failure must not leave delivered media without deletion.
            print(f"[AUTO_DELETE] Notice failed for user={user_id}: {notice_error}")

    if schedule_delete and delay > 0:
        await schedule_persistent_delete(user_id, message_ids, delay)

    return delivered
