# HJ GROUPS OF FILES - Media saving

import asyncio
import requests
import string
import random
from configs import Config
from pyrogram import Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from handlers.helpers import str_to_b64


def generate_random_alphanumeric():
    """Generate a random 8-character alphanumeric string."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(8))


def _user_can_save(user_id: int) -> bool:
    """Empty allow-list means all users are allowed, matching normal FileStore behavior."""
    allowed_users = Config.OTHER_USERS_CAN_SAVE_FILE
    return int(user_id) == Config.BOT_OWNER or not allowed_users or int(user_id) in allowed_users


def get_short(url):
    """Shorten the link when configured; otherwise safely return the original link."""
    if not Config.SHORTLINK_URL or not Config.SHORTLINK_API:
        return url
    try:
        rget = requests.get(
            f"https://{Config.SHORTLINK_URL}/api",
            params={
                "api": Config.SHORTLINK_API,
                "url": url,
                "alias": generate_random_alphanumeric(),
            },
            timeout=15,
        )
        rjson = rget.json()
        if rjson.get("status") == "success" or rget.status_code == 200:
            return rjson.get("shortenedUrl") or url
    except Exception as err:
        print(f"Shortener unavailable, using original link: {err}")
    return url


async def forward_to_channel(bot: Client, message: Message, editable: Message):
    try:
        return await message.forward(Config.DB_CHANNEL)
    except FloodWait as sl:
        await asyncio.sleep(sl.value)
        if Config.LOG_CHANNEL:
            try:
                await bot.send_message(
                    chat_id=int(Config.LOG_CHANNEL),
                    text=f"#FloodWait:\nGot FloodWait of `{str(sl.value)}s` from `{str(editable.chat.id)}` !!",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Ban User", callback_data=f"ban_user_{str(editable.chat.id)}")]
                    ])
                )
            except Exception:
                pass
        return await forward_to_channel(bot, message, editable)


async def save_batch_media_in_channel(bot: Client, editable: Message, message_ids: list):
    try:
        source_user = editable.reply_to_message.from_user
        if source_user is None or not _user_can_save(source_user.id):
            await editable.reply_text("You are not authorized to save files.")
            return

        message_ids_str = ""
        for message_id in message_ids:
            message = await bot.get_messages(chat_id=editable.chat.id, message_ids=message_id)
            sent_message = await forward_to_channel(bot, message, editable)
            if sent_message is None:
                continue
            message_ids_str += f"{str(sent_message.id)} "
            await asyncio.sleep(2)

        if not message_ids_str.strip():
            await editable.edit("No files were available to save in this batch.")
            return

        SaveMessage = await bot.send_message(
            chat_id=Config.DB_CHANNEL,
            text=message_ids_str.strip(),
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Delete Batch", callback_data="closeMessage")
            ]])
        )
        share_link = f"https://telegram.me/{Config.BOT_USERNAME}?start=PredatorHackerzZ_{str_to_b64(str(SaveMessage.id))}"
        short_link = get_short(share_link)

        buttons = [[InlineKeyboardButton("Original Link", url=share_link)]]
        if short_link != share_link:
            buttons[0].append(InlineKeyboardButton("Short Link", url=short_link))

        await editable.edit(
            f"**Batch Files Stored in my Database!**\n\nHere is the Permanent Link of your files: {short_link}\n\n"
            "Just Click the link to get your files!",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True
        )
        if Config.LOG_CHANNEL:
            try:
                await bot.send_message(
                    chat_id=int(Config.LOG_CHANNEL),
                    text=f"#BATCH_SAVE:\n\n[{source_user.first_name}](tg://user?id={source_user.id}) Got Batch Link!",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open Link", url=short_link)]])
                )
            except Exception:
                pass
    except Exception as err:
        await editable.edit(f"Something Went Wrong!\n\n**Error:** `{err}`")
        if Config.LOG_CHANNEL:
            try:
                await bot.send_message(
                    chat_id=int(Config.LOG_CHANNEL),
                    text=f"#ERROR_TRACEBACK:\nGot Error from `{str(editable.chat.id)}` !!\n\n**Traceback:** `{err}`",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Ban User", callback_data=f"ban_user_{str(editable.chat.id)}")]
                    ])
                )
            except Exception:
                pass


async def save_media_in_channel(bot: Client, editable: Message, message: Message):
    try:
        if message.from_user is None or not _user_can_save(message.from_user.id):
            await editable.reply_text("You are not authorized to save files.")
            return

        forwarded_msg = await message.forward(Config.DB_CHANNEL)
        file_er_id = str(forwarded_msg.id)
        if Config.LOG_CHANNEL:
            try:
                await forwarded_msg.reply_text(
                    f"#PRIVATE_FILE:\n\n[{message.from_user.first_name}](tg://user?id={message.from_user.id}) Got File Link!",
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

        share_link = f"https://telegram.me/{Config.BOT_USERNAME}?start=PredatorHackerzZ_{str_to_b64(file_er_id)}"
        short_link = get_short(share_link)
        buttons = [[InlineKeyboardButton("Original Link", url=share_link)]]
        if short_link != share_link:
            buttons[0].append(InlineKeyboardButton("Short Link", url=short_link))

        await editable.edit(
            "**Your File Stored in my Database!**\n\n"
            f"Here is the Permanent Link of your file: {short_link}\n\n"
            "Just Click the link to get your file!",
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
    except FloodWait as sl:
        await asyncio.sleep(sl.value)
        if Config.LOG_CHANNEL:
            try:
                await bot.send_message(
                    chat_id=int(Config.LOG_CHANNEL),
                    text="#FloodWait:\n"
                         f"Got FloodWait of `{str(sl.value)}s` from `{str(editable.chat.id)}` !!",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Ban User", callback_data=f"ban_user_{str(editable.chat.id)}")]
                    ])
                )
            except Exception:
                pass
        await save_media_in_channel(bot, editable, message)
    except Exception as err:
        await editable.edit(f"Something Went Wrong!\n\n**Error:** `{err}`")
        if Config.LOG_CHANNEL:
            try:
                await bot.send_message(
                    chat_id=int(Config.LOG_CHANNEL),
                    text="#ERROR_TRACEBACK:\n"
                         f"Got Error from `{str(editable.chat.id)}` !!\n\n"
                         f"**Traceback:** `{err}`",
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Ban User", callback_data=f"ban_user_{str(editable.chat.id)}")]
                    ])
                )
            except Exception:
                pass
