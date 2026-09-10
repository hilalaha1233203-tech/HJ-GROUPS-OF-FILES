# HJ GROUPS OF FILES - File delivery

import asyncio
import inspect
from pyrogram import Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from configs import Config
from handlers.database import db
from handlers.telegram_api import copy_message as api_copy_message, edit_message_caption as api_edit_message_caption, edit_message_reply_markup as api_edit_message_reply_markup

_DELETE_TASKS = set()
_BATCH_DELETE_CONTEXTS = {}


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
    try:
        await asyncio.sleep(delay)
        clean_ids = [int(mid) for mid in message_ids if mid]
        if clean_ids:
            await bot.delete_messages(chat_id=int(chat_id), message_ids=clean_ids, revoke=True)
            print(f"[AUTO_DELETE] Deleted chat={chat_id} messages={clean_ids}")
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await _delete_delivered_messages(bot, chat_id, message_ids, 0)
    except Exception as err:
        print(f"[AUTO_DELETE] Failed chat={chat_id} messages={message_ids}: {err}")


def _track_delete_task(task):
    _DELETE_TASKS.add(task)
    task.add_done_callback(_DELETE_TASKS.discard)


async def send_delete_notice(bot: Client, user_id: int, delay: int):
    text = build_delete_notice(delay)
    if not text:
        return None
    try:
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
    notice = await send_delete_notice(bot, user_id, delay)
    if notice is not None:
        notice_id = getattr(notice, "id", None) if not isinstance(notice, dict) else notice.get("message_id")
        if notice_id:
            delivered_ids.append(int(notice_id))
    task = asyncio.create_task(_delete_delivered_messages(bot, user_id, delivered_ids, delay))
    _track_delete_task(task)


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
        notice = await send_delete_notice(bot, user_id, delay)
        if notice is not None:
            message_ids.append(getattr(notice, "id", None))

    if schedule_delete and delay > 0:
        task = asyncio.create_task(_delete_delivered_messages(bot, user_id, message_ids, delay))
        _track_delete_task(task)

    return delivered
