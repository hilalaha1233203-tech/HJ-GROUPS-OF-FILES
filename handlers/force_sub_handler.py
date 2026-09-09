# (c) @PredatorHackerzZ

import asyncio
from typing import Union
from configs import Config
from pyrogram import Client
from pyrogram.errors import FloodWait, UserNotParticipant
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message


def _channel_id(value):
    """Normalize a configured channel username or numeric chat id."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if value.startswith("-100"):
        return int(value)
    return value


async def get_invite_link(bot: Client, chat_id: Union[str, int]):
    while True:
        try:
            return await bot.create_chat_invite_link(chat_id=chat_id)
        except FloodWait as e:
            print(f"Sleep of {e.value}s caused by FloodWait ...")
            await asyncio.sleep(e.value)


async def handle_force_sub(bot: Client, cmd: Message):
    channel_chat_id = _channel_id(Config.UPDATES_CHANNEL)
    if channel_chat_id is None:
        return 200

    # For normal messages use from_user; for a Refresh callback message,
    # from_user is the bot, so the private chat id identifies the user.
    user_id = cmd.from_user.id if cmd.from_user else cmd.chat.id

    try:
        member = await bot.get_chat_member(
            chat_id=channel_chat_id,
            user_id=user_id,
        )
        status = str(member.status).lower()
        if status in {"kicked", "banned"}:
            await bot.send_message(
                chat_id=user_id,
                text="Sorry, you are banned from using this bot.",
                disable_web_page_preview=True,
            )
            return 400
        return 200

    except UserNotParticipant:
        pass
    except Exception as err:
        # Never silently bypass force-sub when Telegram cannot verify membership.
        print(f"Force-sub check failed for channel {channel_chat_id}: {err}")
        try:
            await bot.send_message(
                chat_id=user_id,
                text="Force Subscribe is temporarily unavailable. Please try again later.",
            )
        except Exception:
            pass
        return 400

    try:
        invite_link = await get_invite_link(bot, channel_chat_id)
    except Exception as err:
        print(f"Unable to create force-sub invite link for {channel_chat_id}: {err}")
        try:
            await bot.send_message(
                chat_id=user_id,
                text="The Updates Channel is not configured correctly. Please contact the bot owner.",
            )
        except Exception:
            pass
        return 400

    await bot.send_message(
        chat_id=user_id,
        text="**Please join the Updates Channel to use this bot.**\n\n"
             "After joining, press Refresh to continue.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Updates Channel", url=invite_link.invite_link)],
            [InlineKeyboardButton("🔄 Refresh", callback_data="refreshForceSub")],
        ]),
    )
    return 400
