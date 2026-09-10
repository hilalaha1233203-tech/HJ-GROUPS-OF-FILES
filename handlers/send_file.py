# HJ GROUPS OF FILES - File delivery

import asyncio
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from configs import Config
from handlers.database import db
from handlers.telegram_api import copy_message as api_copy_message

# Keep strong references to scheduled deletion tasks until they finish.
_DELETE_TASKS = set()


async def reply_forward(message: Message, file_id: int, delay: int):
    try:
        if delay > 0:
            minutes = max(1, delay // 60)
            return await message.reply_text(
                f"Files will be deleted in {minutes} minute(s). Please forward and save them.",
                disable_web_page_preview=True,
                quote=True,
            )
        return await message.reply_text(
            "Files are not scheduled for automatic deletion. Please forward and save them.",
            disable_web_page_preview=True,
            quote=True,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await reply_forward(message, file_id, delay)


async def media_forward(bot: Client, user_id: int, file_id: int, channel_id=None):
    try:
        channel_id = channel_id or await db.get_db_channel_id()
        if channel_id is None:
            raise RuntimeError("Storage channel is not configured.")

        # Telegram's protected-content flag is the only Bot API mechanism that
        # disables forwarding/saving/download actions for delivered media.
        # When the admin enables either protection setting, enable it on the
        # actual delivered message (not the storage-channel copy).
        protect_content = await db.get_protect_content()
        if Config.FORWARD_AS_COPY:
            return await bot.copy_message(
                chat_id=user_id,
                from_chat_id=channel_id,
                message_id=file_id,
                protect_content=protect_content,
            )
        return await bot.forward_messages(
            chat_id=user_id,
            from_chat_id=channel_id,
            message_ids=file_id,
            protect_content=protect_content,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await media_forward(bot, user_id, file_id, channel_id)
    except Exception as pyrogram_error:
        try:
            protection = await db.get_protection_settings()
            protect = protection["protect_forward"] or protection["protect_download"]
            return await api_copy_message(
                chat_id=user_id,
                from_chat_id=channel_id,
                message_id=file_id,
                protect_content=protect,
            )
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


async def send_media_and_reply(bot: Client, user_id: int, file_id: int, channel_id=None):
    sent_message = await media_forward(bot, user_id, file_id, channel_id)
    if sent_message is None:
        return

    # Pyrogram can return a Message or a list/tuple depending on the method.
    if isinstance(sent_message, (list, tuple)):
        delivered = [m for m in sent_message if m is not None]
    else:
        delivered = [sent_message]

    delay = await db.get_auto_delete_seconds()
    message_ids = [getattr(m, "id", None) for m in delivered]

    # Keep the countdown notice separate, then remove both the media and notice
    # after the configured delay. This makes the setting observable to users.
    notice = await reply_forward(delivered[0], file_id, delay)
    if notice is not None:
        message_ids.append(getattr(notice, "id", None))

    if delay > 0:
        task = asyncio.create_task(
            _delete_delivered_messages(bot, user_id, message_ids, delay)
        )
        _track_delete_task(task)
