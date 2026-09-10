# HJ GROUPS OF FILES

import os
import asyncio
import traceback
import time
import re
from binascii import Error
from pyrogram import Client, enums, filters, idle
from pyrogram.errors import UserNotParticipant, FloodWait, QueryIdInvalid
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, BotCommand
from configs import Config
from handlers.database import db
from handlers.add_user_to_db import add_user_to_database
from handlers.send_file import send_media_and_reply
from handlers.helpers import b64_to_str, str_to_b64
from handlers.check_user_status import handle_user_status
from handlers.broadcast_handlers import main_broadcast_handler
from handlers.save_media import save_media_in_channel, save_batch_media_in_channel, get_short

MediaList = {}

Bot = Client(
    name=Config.BOT_USERNAME,
    in_memory=True,
    bot_token=Config.BOT_TOKEN,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH
)


def make_share_link(message_id: int, channel_id=None) -> str:
    payload = str(int(message_id)) if channel_id is None else f"{int(channel_id)}|{int(message_id)}"
    token = str_to_b64(payload)
    return f"https://telegram.me/{Config.BOT_USERNAME}?start=PredatorHackerzZ_{token}"


def parse_db_message_link(text: str):
    """Return a DB-channel message id when text contains a Telegram link to this DB channel."""
    if not text:
        return None

    match = re.search(r"https?://(?:t\.me|telegram\.me)/c/(\d+)/(\d+)", text, flags=re.I)
    if match:
        channel_part = match.group(1)
        message_id = int(match.group(2))
        try:
            channel_id = int(f"-100{channel_part}")
        except ValueError:
            return None
        return (channel_id, message_id)

    match = re.search(r"https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/([0-9]+)", text, flags=re.I)
    if match and Config.BOT_USERNAME:
        return ("@" + match.group(1), int(match.group(2)))
    return None


async def resolve_existing_db_message(bot: Client, message: Message):
    """Resolve an already-stored DB-channel message from a forwarded message or Telegram link."""
    forwarded_chat = getattr(message, "forward_from_chat", None)
    forwarded_message_id = getattr(message, "forward_from_message_id", None)
    db_channel_id = await db.get_db_channel_id()

    if forwarded_chat is not None and forwarded_message_id:
        source_channel_id = int(forwarded_chat.id)
        if db_channel_id is None:
            if int(message.from_user.id) != int(Config.BOT_OWNER):
                return None
            # First owner-forward automatically configures the storage channel.
            await bot.get_chat(source_channel_id)
            await db.set_db_channel_id(source_channel_id)
            return int(forwarded_message_id)
        if source_channel_id == int(db_channel_id):
            return int(forwarded_message_id)

    parsed = parse_db_message_link(message.text or message.caption or "")
    if parsed is None:
        return None

    channel_ref, message_id = parsed
    if isinstance(channel_ref, int):
        if db_channel_id is None:
            return None
        return int(message_id) if int(channel_ref) == int(db_channel_id) else None

    try:
        chat = await bot.get_chat(channel_ref)
        if db_channel_id is not None and int(chat.id) == int(db_channel_id):
            return int(message_id)
    except Exception:
        return None
    return None


async def send_existing_db_link(bot: Client, cmd: Message, message_id: int):
    """Validate an existing DB message and return a FileStore link without re-uploading it."""
    channel_id = await db.get_db_channel_id()
    if channel_id is None:
        raise RuntimeError("Storage channel is not configured. Forward one message from the private storage channel to the bot first.")
    db_message = await bot.get_messages(chat_id=channel_id, message_ids=int(message_id))
    if not db_message or int(db_message.id) <= 0:
        raise ValueError("DB channel message not found")

    share_link = make_share_link(int(db_message.id), channel_id)
    short_link = get_short(share_link)
    buttons = [[InlineKeyboardButton("Original Link", url=share_link)]]
    if short_link != share_link:
        buttons[0].append(InlineKeyboardButton("Short Link", url=short_link))

    await cmd.reply_text(
        "**Existing Database File Found!**\n\n"
        f"Here is the Permanent Link of your file: {short_link}\n\n"
        "This file was already stored in the configured Database Channel, so it was not uploaded again.",
        reply_markup=InlineKeyboardMarkup(buttons),
        disable_web_page_preview=True,
    )


@Bot.on_message(filters.private)
async def _(bot: Client, cmd: Message):
    # Run user status checks but don't block command handlers below.
    await handle_user_status(bot, cmd)
    try:
        # Allow other handlers (like command-specific ones) to run after status handling.
        await cmd.continue_propagation()
    except Exception:
        # continue_propagation may not be available in older pyrogram versions; ignore if so.
        pass


@Bot.on_message(filters.command("start") & filters.private)
async def start(bot: Client, cmd: Message):
    if cmd.from_user.id in Config.BANNED_USERS:
        await cmd.reply_text("Sorry, You are banned.")
        return

    usr_cmd = cmd.text.split("_", 1)[-1]
    if usr_cmd == "/start":
        await add_user_to_database(bot, cmd)
        await cmd.reply_text(
            Config.HOME_TEXT.format(cmd.from_user.first_name, cmd.from_user.id),
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Commands", callback_data="commands")],
                [InlineKeyboardButton("About Bot", callback_data="aboutbot"), InlineKeyboardButton("Close 🚪", callback_data="closeMessage")]
            ])
        )
        return

    try:
        try:
            decoded = b64_to_str(usr_cmd)
            if "|" in decoded:
                channel_part, message_part = decoded.split("|", 1)
                channel_id = int(channel_part)
                file_id = int(message_part)
            else:
                channel_id = None
                file_id = int(decoded.split("_")[-1])
        except (Error, UnicodeDecodeError, ValueError):
            raw = usr_cmd.split("_")[-1]
            if "|" in raw:
                channel_part, message_part = raw.split("|", 1)
                channel_id = int(channel_part)
                file_id = int(message_part)
            else:
                channel_id = None
                file_id = int(raw)

        if channel_id is None:
            channel_id = await db.get_db_channel_id()
        if channel_id is None:
            raise RuntimeError("Storage channel is not configured. Owner must forward one message from the private storage channel to the bot first.")
        get_message = await bot.get_messages(chat_id=channel_id, message_ids=file_id)
        message_ids = []
        if get_message.text:
            message_ids = [x for x in get_message.text.split() if x]
            await cmd.reply_text(
                text=f"**Total Files:** `{len(message_ids)}`",
                quote=True,
                disable_web_page_preview=True
            )
        else:
            message_ids.append(int(get_message.id))

        for message_id in message_ids:
            await send_media_and_reply(bot, user_id=cmd.from_user.id, file_id=int(message_id), channel_id=channel_id)
    except Exception as err:
        await cmd.reply_text(f"Something went wrong!\n\n**Error:** `{err}`")


@Bot.on_message(
    (filters.document | filters.video | filters.audio | filters.photo | (filters.text & ~filters.command()))
    & filters.private
)
async def main(bot: Client, message: Message):
    if message.chat.type == enums.ChatType.PRIVATE:
        await add_user_to_database(bot, message)

        if message.from_user.id in Config.BANNED_USERS:
            await message.reply_text(
                "Sorry, You are banned!\n\nContact the bot owner for help.",
                disable_web_page_preview=True
            )
            return

        try:
            existing_message_id = await resolve_existing_db_message(bot, message)
            if existing_message_id is not None:
                await send_existing_db_link(bot, message, existing_message_id)
                return
        except Exception as err:
            await message.reply_text(
                "Could not read that Database Channel message.\n\n"
                f"**Error:** `{err}`\n\n"
                "Make sure the bot is an admin/member of the configured Database Channel."
            )
            return

        if message.text and ("t.me/" in message.text.lower() or "telegram.me/" in message.text.lower()):
            await message.reply_text(
                "That message link is not from the configured Database Channel.\n\n"
                f"Configured DB Channel: `{await db.get_db_channel_id()}`"
            )
            return

        if message.media:
            await message.reply_text(
                text="**Choose an option from below:**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📦 Save in Batch", callback_data="addToBatchTrue")],
                    [InlineKeyboardButton("🔗 Get Sharable Link", callback_data="addToBatchFalse")]
                ]),
                quote=True,
                disable_web_page_preview=True
            )

    elif message.chat.type == enums.ChatType.CHANNEL:
        updates_id = None
        log_id = None
        if Config.UPDATES_CHANNEL:
            try:
                updates_id = int(Config.UPDATES_CHANNEL)
            except ValueError:
                updates_id = None
        if Config.LOG_CHANNEL:
            try:
                log_id = int(Config.LOG_CHANNEL)
            except ValueError:
                log_id = None

        if message.chat.id in {x for x in (updates_id, log_id) if x is not None}:
            return
        if message.forward_from_chat or message.forward_from:
            return
        if int(message.chat.id) in Config.BANNED_CHAT_IDS:
            await bot.leave_chat(message.chat.id)
            return

        try:
            channel_id = await db.get_db_channel_id()
            if channel_id is None:
                raise RuntimeError("Storage channel is not configured. Owner must forward one message from the private storage channel to the bot first.")
            forwarded_msg = await message.forward(channel_id)
            file_er_id = str(forwarded_msg.id)
            share_link = make_share_link(int(file_er_id))
            ch_edit = await bot.edit_message_reply_markup(
                message.chat.id,
                message.id,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Get Sharable Link", url=share_link)]
                ])
            )
            if message.chat.username:
                channel_url = f"https://t.me/{message.chat.username}/{ch_edit.id}"
            else:
                private_ch = str(message.chat.id)[4:]
                channel_url = f"https://t.me/c/{private_ch}/{ch_edit.id}"
            try:
                await forwarded_msg.reply_text(
                    f"#CHANNEL_BUTTON:\n\n[{message.chat.title}]({channel_url}) Channel's Broadcasted File's Button Added!"
                )
            except Exception:
                pass
        except FloodWait as sl:
            await asyncio.sleep(sl.value)
            if Config.LOG_CHANNEL:
                try:
                    await bot.send_message(
                        chat_id=int(Config.LOG_CHANNEL),
                        text=f"#FloodWait:\nGot FloodWait of `{str(sl.value)}s` from `{str(message.chat.id)}` !!",
                        disable_web_page_preview=True
                    )
                except Exception:
                    pass
        except Exception as err:
            try:
                await bot.leave_chat(message.chat.id)
            except Exception:
                pass
            if Config.LOG_CHANNEL:
                try:
                    await bot.send_message(
                        chat_id=int(Config.LOG_CHANNEL),
                        text=f"#ERROR_TRACEBACK:\nGot Error from `{str(message.chat.id)}` !!\n\n**Traceback:** `{err}`",
                        disable_web_page_preview=True
                    )
                except Exception:
                    pass


async def _reply_saved_link(bot, m):
    if not m.reply_to_message:
        await m.reply_text("Reply to a file/media/message and use this command.")
        return
    status = await m.reply_text("⏳ Saving...")
    await save_media_in_channel(bot, status, m.reply_to_message)


@Bot.on_message(filters.private & filters.command("genlink"))
async def genlink(bot, m):
    await _reply_saved_link(bot, m)


@Bot.on_message(filters.private & filters.command("batch"))
async def batch(bot, m):
    if not m.reply_to_message or len(m.command) < 3:
        await m.reply_text("Usage: reply to a channel message and use `/batch start_id end_id`.")
        return
    try:
        ids = list(range(int(m.command[1]), int(m.command[2]) + 1))
    except ValueError:
        await m.reply_text("Batch message IDs must be numbers.")
        return
    status = await m.reply_text(f"⏳ Saving {len(ids)} messages...")
    await save_batch_media_in_channel(bot, status, ids)


@Bot.on_message(filters.private & filters.command("custom_batch"))
async def custom_batch(bot, m):
    if not m.reply_to_message or len(m.command) < 2:
        await m.reply_text("Usage: reply to a channel message and use `/custom_batch 101 105 110`.")
        return
    try:
        ids = [int(x) for x in m.command[1:]]
    except ValueError:
        await m.reply_text("Message IDs must be numbers.")
        return
    status = await m.reply_text(f"⏳ Saving {len(ids)} selected messages...")
    await save_batch_media_in_channel(bot, status, ids)


@Bot.on_message(filters.private & filters.command("shortener"))
async def shortener(bot, m):
    url = " ".join(m.command[1:]).strip()
    if not url and m.reply_to_message:
        url = (m.reply_to_message.text or m.reply_to_message.caption or "").strip()
    if not url:
        await m.reply_text("Usage: `/shortener https://example.com/link`")
        return
    short = get_short(url)
    await m.reply_text(f"🔗 **Shortened Link:**\n{short}", disable_web_page_preview=True)


@Bot.on_message(filters.private & filters.command("special_link"))
async def special_link(bot, m):
    await batch(bot, m)


@Bot.on_message(filters.private & filters.command("universal_link"))
async def universal_link(bot, m):
    await batch(bot, m)


@Bot.on_message(filters.private & filters.command("ban"))
async def ban_alias(bot, m):
    if not Config.BOT_OWNER or int(m.from_user.id) != int(Config.BOT_OWNER):
        await m.reply_text("⛔ Owner/Admin Only")
        return
    if len(m.command) < 4:
        await m.reply_text("Usage: `/ban user_id days reason`")
        return
    await db.ban_user(int(m.command[1]), int(m.command[2]), " ".join(m.command[3:]))
    await m.reply_text("✅ User banned.")


@Bot.on_message(filters.private & filters.command("unban"))
async def unban_alias(bot, m):
    if not Config.BOT_OWNER or int(m.from_user.id) != int(Config.BOT_OWNER):
        await m.reply_text("⛔ Owner/Admin Only")
        return
    if len(m.command) < 2:
        await m.reply_text("Usage: `/unban user_id`")
        return
    await db.remove_ban(int(m.command[1]))
    await m.reply_text("✅ User unbanned.")


@Bot.on_message(filters.private & filters.command("broadcast") & filters.user(Config.BOT_OWNER) & filters.reply)
async def broadcast_handler_open(_, m: Message):
    await main_broadcast_handler(m, db)


@Bot.on_message(filters.private & filters.command("settings"))
async def settings(_, m: Message):
    # Do not put the owner check in the Pyrogram filter: that silently drops
    # the command when BOT_OWNER is wrong. Always reply with a useful result.
    if not Config.BOT_OWNER or int(m.from_user.id) != int(Config.BOT_OWNER):
        configured = Config.BOT_OWNER if Config.BOT_OWNER else "NOT SET"
        await m.reply_text(
            "⛔ **Owner/Admin Only**\n\n"
            f"Your Telegram ID: `{m.from_user.id}`\n"
            f"Configured BOT_OWNER: `{configured}`\n\n"
            "If you are the owner, set **BOT_OWNER** in Voroa to your Telegram user ID, then redeploy/restart the bot."
        )
        return
    await show_settings(m)


async def show_settings(message):
    delay = await db.get_auto_delete_seconds()
    current = "Disabled" if delay <= 0 else f"{delay // 60} minute(s)"
    protection = await db.get_protection_settings()
    forward_state = "ON" if protection["protect_forward"] else "OFF"
    download_state = "ON" if protection["protect_download"] else "OFF"
    await message.reply_text(
        "**⚙️ HJ GROUPS STORE KEEPER — SETTINGS**\n\n"
        f"**Auto-delete delivered files:** `{current}`\n"
        f"**Restrict Forwarding:** `{forward_state}`\n"
        f"**Restrict Saving / Download:** `{download_state}`\n\n"
        "Telegram uses one native content-protection switch for forwarding and saving. "
        "If either protection is ON, delivered files use Telegram's protected-content mode.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("5 min", callback_data="setdel_300"),
             InlineKeyboardButton("15 min", callback_data="setdel_900"),
             InlineKeyboardButton("30 min", callback_data="setdel_1800")],
            [InlineKeyboardButton("1 hour", callback_data="setdel_3600"),
             InlineKeyboardButton("6 hours", callback_data="setdel_21600"),
             InlineKeyboardButton("24 hours", callback_data="setdel_86400")],
            [InlineKeyboardButton("♾️ Disable Timer", callback_data="setdel_0")],
            [InlineKeyboardButton(f"🚫 Forward: {forward_state}", callback_data="toggle_protect_forward"),
             InlineKeyboardButton(f"🚫 Download: {download_state}", callback_data="toggle_protect_download")],
            [InlineKeyboardButton("Close", callback_data="closeMessage")]
        ])
    )


@Bot.on_message(filters.private & filters.command("status") & filters.user(Config.BOT_OWNER))
async def sts(_, m: Message):
    total_users = await db.total_users_count()
    await m.reply_text(text=f"**Total Users in DB:** `{total_users}`", quote=True)


@Bot.on_message(filters.private & filters.command("ban_user") & filters.user(Config.BOT_OWNER))
async def ban(c: Client, m: Message):
    if len(m.command) < 4:
        await m.reply_text(
            "Usage: `/ban_user user_id ban_duration ban_reason`\n\n"
            "Example: `/ban_user 1234567 28 You misused me.`",
            quote=True
        )
        return
    try:
        user_id = int(m.command[1])
        ban_duration = int(m.command[2])
        ban_reason = ' '.join(m.command[3:])
        ban_log_text = f"Banning user {user_id} for {ban_duration} days for the reason {ban_reason}."
        try:
            await c.send_message(
                user_id,
                f"You are banned to use this bot for **{ban_duration}** day(s) for the reason __{ban_reason}__\n\n**Message from the admin**"
            )
            ban_log_text += '\n\nUser notified successfully!'
        except Exception:
            ban_log_text += f"\n\nUser notification failed!\n\n`{traceback.format_exc()}`"
        await db.ban_user(user_id, ban_duration, ban_reason)
        await m.reply_text(ban_log_text, quote=True)
    except Exception:
        await m.reply_text(f"Error occurred! Traceback given below\n\n`{traceback.format_exc()}`", quote=True)


@Bot.on_message(filters.private & filters.command("unban_user") & filters.user(Config.BOT_OWNER))
async def unban(c: Client, m: Message):
    if len(m.command) < 2:
        await m.reply_text("Usage: `/unban_user user_id`", quote=True)
        return
    try:
        user_id = int(m.command[1])
        unban_log_text = f"Unbanning user {user_id}"
        try:
            await c.send_message(user_id, "Your ban was lifted!")
            unban_log_text += '\n\nUser notified successfully!'
        except Exception:
            unban_log_text += f"\n\nUser notification failed!\n\n`{traceback.format_exc()}`"
        await db.remove_ban(user_id)
        await m.reply_text(unban_log_text, quote=True)
    except Exception:
        await m.reply_text(f"Error occurred! Traceback given below\n\n`{traceback.format_exc()}`", quote=True)


@Bot.on_message(filters.private & filters.command("banned_users") & filters.user(Config.BOT_OWNER))
async def _banned_users(_, m: Message):
    all_banned_users = await db.get_all_banned_users()
    banned_usr_count = 0
    text = ''
    async for banned_user in all_banned_users:
        user_id = banned_user['id']
        ban_duration = banned_user['ban_status']['ban_duration']
        banned_on = banned_user['ban_status']['banned_on']
        ban_reason = banned_user['ban_status']['ban_reason']
        banned_usr_count += 1
        text += f"> **user_id**: `{user_id}`, **Ban Duration**: `{ban_duration}`, **Banned on**: `{banned_on}`, **Reason**: `{ban_reason}`\n\n"

    reply_text = f"Total banned user(s): `{banned_usr_count}`\n\n{text}"
    if len(reply_text) > 4096:
        with open('banned-users.txt', 'w', encoding='utf-8') as f:
            f.write(reply_text)
        await m.reply_document('banned-users.txt', True)
        os.remove('banned-users.txt')
        return
    await m.reply_text(reply_text, True)


@Bot.on_message(filters.private & filters.command("clear_batch"))
async def clear_user_batch(_, m: Message):
    MediaList[str(m.from_user.id)] = []
    await m.reply_text("Cleared your batch files successfully!")


@Bot.on_callback_query()
async def button(bot: Client, cmd: CallbackQuery):
    cb_data = cmd.data

    if cb_data == "commands":
        await cmd.message.edit(
            Config.COMMANDS_TEXT,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Go Home", callback_data="gotohome")],
                [InlineKeyboardButton("About Bot", callback_data="aboutbot")]
            ])
        )

    elif cb_data == "aboutbot":
        await cmd.message.edit(
            Config.ABOUT_BOT_TEXT,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Commands", callback_data="commands")],
                [InlineKeyboardButton("🏠 Go Home", callback_data="gotohome")]
            ])
        )

    elif cb_data == "gotohome":
        await cmd.message.edit(
            Config.HOME_TEXT.format(cmd.message.chat.first_name, cmd.message.chat.id),
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Commands", callback_data="commands")],
                [InlineKeyboardButton("About Bot", callback_data="aboutbot"), InlineKeyboardButton("Close 🚪", callback_data="closeMessage")]
            ])
        )

    elif cb_data.startswith("ban_user_"):
        if not Config.UPDATES_CHANNEL:
            await cmd.answer("No Updates Channel configured.", show_alert=True)
            return
        if int(cmd.from_user.id) != Config.BOT_OWNER:
            await cmd.answer("You are not allowed to do that!", show_alert=True)
            return
        try:
            channel_id = int(Config.UPDATES_CHANNEL)
            user_id = int(cb_data.split("_", 2)[-1])
            await bot.ban_chat_member(chat_id=channel_id, user_id=user_id)
            await cmd.answer("User banned from Updates Channel!", show_alert=True)
        except Exception as e:
            await cmd.answer(f"Can't ban user.\n\nError: {e}", show_alert=True)

    elif cb_data == "addToBatchTrue":
        if MediaList.get(str(cmd.from_user.id)) is None:
            MediaList[str(cmd.from_user.id)] = []
        if cmd.message.reply_to_message is None:
            await cmd.answer("Source file not found.", show_alert=True)
            return
        MediaList[str(cmd.from_user.id)].append(cmd.message.reply_to_message.id)
        await cmd.message.edit(
            "File Saved in Batch!\n\nPress below button to get batch link.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Get Batch Link", callback_data="getBatchLink")],
                [InlineKeyboardButton("Close Message", callback_data="closeMessage")]
            ])
        )

    elif cb_data == "addToBatchFalse":
        await save_media_in_channel(bot, editable=cmd.message, message=cmd.message.reply_to_message)

    elif cb_data == "getBatchLink":
        message_ids = MediaList.get(str(cmd.from_user.id))
        if not message_ids:
            await cmd.answer("Batch List Empty!", show_alert=True)
            return
        await cmd.message.edit("Please wait, generating batch link ...")
        await save_batch_media_in_channel(bot=bot, editable=cmd.message, message_ids=message_ids)
        MediaList[str(cmd.from_user.id)] = []

    elif cb_data.startswith("setdel_"):
        if not Config.BOT_OWNER or int(cmd.from_user.id) != Config.BOT_OWNER:
            await cmd.answer("You are not allowed to change bot settings.", show_alert=True)
            return
        try:
            seconds = int(cb_data.split("_", 1)[1])
            await db.set_auto_delete_seconds(seconds)
            label = "Disabled" if seconds == 0 else f"{seconds // 60} minute(s)"
            await cmd.message.edit(
                "**⚙️ HJ GROUPS BOT SETTINGS**\n\n"
                f"**Auto-delete delivered files:** `{label}`\n\n"
                "Setting saved successfully.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Change Setting", callback_data="open_settings")],
                    [InlineKeyboardButton("Close", callback_data="closeMessage")]
                ])
            )
            await cmd.answer("Auto-delete setting saved.")
        except Exception as err:
            await cmd.answer(f"Could not save setting: {err}", show_alert=True)

    elif cb_data in {"toggle_protect_forward", "toggle_protect_download"}:
        if not Config.BOT_OWNER or int(cmd.from_user.id) != Config.BOT_OWNER:
            await cmd.answer("You are not allowed to change bot settings.", show_alert=True)
            return
        key = "protect_forward" if cb_data == "toggle_protect_forward" else "protect_download"
        current_settings = await db.get_protection_settings()
        new_value = not current_settings[key]
        try:
            await db.set_protection_setting(key, new_value)
            await cmd.answer("Protection setting saved.")
            delay = await db.get_auto_delete_seconds()
            current = "Disabled" if delay <= 0 else f"{delay // 60} minute(s)"
            protection = await db.get_protection_settings()
            forward_state = "ON" if protection["protect_forward"] else "OFF"
            download_state = "ON" if protection["protect_download"] else "OFF"
            await cmd.message.edit(
                "**⚙️ HJ GROUPS STORE KEEPER — SETTINGS**\n\n"
                f"**Auto-delete delivered files:** `{current}`\n"
                f"**Restrict Forwarding:** `{forward_state}`\n"
                f"**Restrict Saving / Download:** `{download_state}`\n\n"
                "If either protection is ON, Telegram protected-content mode is enabled.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("5 min", callback_data="setdel_300"),
                     InlineKeyboardButton("15 min", callback_data="setdel_900"),
                     InlineKeyboardButton("30 min", callback_data="setdel_1800")],
                    [InlineKeyboardButton("1 hour", callback_data="setdel_3600"),
                     InlineKeyboardButton("6 hours", callback_data="setdel_21600"),
                     InlineKeyboardButton("24 hours", callback_data="setdel_86400")],
                    [InlineKeyboardButton("♾️ Disable Timer", callback_data="setdel_0")],
                    [InlineKeyboardButton(f"🚫 Forward: {forward_state}", callback_data="toggle_protect_forward"),
                     InlineKeyboardButton(f"🚫 Download: {download_state}", callback_data="toggle_protect_download")],
                    [InlineKeyboardButton("Close", callback_data="closeMessage")]
                ])
            )
        except Exception as err:
            await cmd.answer(f"Could not save setting: {err}", show_alert=True)

    elif cb_data == "open_settings":
        if not Config.BOT_OWNER or int(cmd.from_user.id) != Config.BOT_OWNER:
            await cmd.answer("You are not allowed to open bot settings.", show_alert=True)
            return
        await show_settings(cmd.message)

    elif cb_data == "closeMessage":
        await cmd.message.delete(True)

    try:
        await cmd.answer()
    except QueryIdInvalid:
        pass


async def validate_db_channel_access():
    """Resolve the DB channel at startup so private-channel peer problems are detected early."""
    try:
        chat = await Bot.get_chat(Config.DB_CHANNEL)
        print(
            f"[DB_CHANNEL] Connected: id={chat.id} title={getattr(chat, 'title', '')!r}"
        )
        return True
    except Exception as err:
        print(
            "[DB_CHANNEL] ERROR: Unable to access the configured Database Channel "
            f"{Config.DB_CHANNEL}: {err}"
        )
        print(
            "[DB_CHANNEL] The bot must be a member/admin of that private channel "
            "and the DB_CHANNEL value must be the correct -100... channel ID."
        )
        return False


async def recover_storage_channels():
    try:
        stored = await db.get_storage_channels()
    except Exception as err:
        print(f"Storage settings read failed: {err}")
        stored = []
    if Config.DB_CHANNEL:
        stored.append(int(Config.DB_CHANNEL))
    seen=[]
    for cid in stored:
        if cid in seen:
            continue
        try:
            chat = await Bot.get_chat(int(cid))
            if chat.type in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
                seen.append(int(chat.id))
        except Exception as err:
            print(f"Storage channel {cid} unavailable: {err}")
    if seen:
        try:
            await db.set_storage_channels(seen)
        except Exception as err:
            print(f"Storage channel persistence failed: {err}")
    return seen


async def setup_bot_commands():
    await Bot.set_bot_commands([
        BotCommand("start", "Start the bot / open file links"),
        BotCommand("genlink", "Store a single message or file"),
        BotCommand("batch", "Store multiple channel messages"),
        BotCommand("custom_batch", "Store selected messages"),
        BotCommand("shortener", "Shorten a shareable link"),
        BotCommand("settings", "Customize bot settings"),
        BotCommand("clear_batch", "Clear your current batch"),
        BotCommand("special_link", "Moderator: create an editable link"),
        BotCommand("universal_link", "Moderator: create a universal link"),
        BotCommand("broadcast", "Admin: broadcast a replied message"),
        BotCommand("ban", "Moderator: ban a user"),
        BotCommand("unban", "Moderator: unban a user"),
        BotCommand("status", "Admin: show total users"),
        BotCommand("ban_user", "Admin: ban a user"),
        BotCommand("unban_user", "Admin: unban a user"),
        BotCommand("banned_users", "Admin: list banned users"),
    ])


async def run_bot():
    await Bot.start()
    await validate_db_channel_access()
    await recover_storage_channels()
    await setup_bot_commands()
    print(f"[{Config.BOT_USERNAME}] Bot started successfully")
    await idle()
    await Bot.stop()


if __name__ == "__main__":
    while True:
        try:
            Bot.run(run_bot())
            break
        except FloodWait as e:
            wait_seconds = max(int(e.value), 60)
            print(f"[{Config.BOT_USERNAME}] Telegram FloodWait during authorization. Waiting {wait_seconds} seconds before retry.")
            time.sleep(wait_seconds)
        except Exception:
            raise
