# HJ GROUPS OF FILES - File delivery

import asyncio
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from configs import Config
from handlers.database import db


async def reply_forward(message: Message, file_id: int, delay: int):
    try:
        if delay > 0:
            minutes = max(1, delay // 60)
            await message.reply_text(
                f"Files will be deleted in {minutes} minute(s). Please forward and save them.",
                disable_web_page_preview=True,
                quote=True,
            )
        else:
            await message.reply_text(
                "Files are not scheduled for automatic deletion. Please forward and save them.",
                disable_web_page_preview=True,
                quote=True,
            )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        await reply_forward(message, file_id, delay)


async def media_forward(bot: Client, user_id: int, file_id: int):
    try:
        protect_content = await db.get_protect_content()
        if Config.FORWARD_AS_COPY:
            return await bot.copy_message(
                chat_id=user_id,
                from_chat_id=Config.DB_CHANNEL,
                message_id=file_id,
                protect_content=protect_content,
            )
        return await bot.forward_messages(
            chat_id=user_id,
            from_chat_id=Config.DB_CHANNEL,
            message_ids=file_id,
            protect_content=protect_content,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await media_forward(bot, user_id, file_id)


async def delete_after_delay(message, delay):
    if delay <= 0:
        return
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except Exception as err:
        print(f"Auto-delete failed: {err}")


async def send_media_and_reply(bot: Client, user_id: int, file_id: int):
    sent_message = await media_forward(bot, user_id, file_id)
    if sent_message is None:
        return
    delay = await db.get_auto_delete_seconds()
    await reply_forward(sent_message, file_id, delay)
    if delay > 0:
        asyncio.create_task(delete_after_delay(sent_message, delay))
