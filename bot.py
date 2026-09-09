# HJ GROUPS OF FILES

import os
import asyncio
import traceback
import time
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
from handlers.force_sub_handler import handle_force_sub
from handlers.broadcast_handlers import main_broadcast_handler
from handlers.save_media import save_media_in_channel, save_batch_media_in_channel

MediaList = {}

Bot = Client(
    name=Config.BOT_USERNAME,
    in_memory=True,
    bot_token=Config.BOT_TOKEN,
    api_id=Config.API_ID,
    api_hash=Config.API_HASH
)


@Bot.on_message(filters.private)
async def _(bot: Client, cmd: Message):
    await handle_user_status(bot, cmd)


@Bot.on_message(filters.command("start") & filters.private)
async def start(bot: Client, cmd: Message):
    if cmd.from_user.id in Config.BANNED_USERS:
        await cmd.reply_text("Sorry, You are banned.")
        return

    if Config.UPDATES_CHANNEL is not None:
        back = await handle_force_sub(bot, cmd)
        if back == 400:
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
            file_id = int(b64_to_str(usr_cmd).split("_")[-1])
        except (Error, UnicodeDecodeError):
            file_id = int(usr_cmd.split("_")[-1])

        get_message = await bot.get_messages(chat_id=Config.DB_CHANNEL, message_ids=file_id)
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
            await send_media_and_reply(bot, user_id=cmd.from_user.id, file_id=int(message_id))
    except Exception as err:
        await cmd.reply_text(f"Something went wrong!\n\n**Error:** `{err}`")


@Bot.on_message((filters.document | filters.video | filters.audio | filters.photo) & ~filters.chat(Config.DB_CHANNEL))
async def main(bot: Client, message: Message):
    if message.chat.type == enums.ChatType.PRIVATE:
        await add_user_to_database(bot, message)

        if Config.UPDATES_CHANNEL is not None:
            back = await handle_force_sub(bot, message)
            if back == 400:
                return

        if message.from_user.id in Config.BANNED_USERS:
            await message.reply_text(
                "Sorry, You are banned!\n\nContact the bot owner for help.",
                disable_web_page_preview=True
            )
            return

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
            forwarded_msg = await message.forward(Config.DB_CHANNEL)
            file_er_id = str(forwarded_msg.id)
            share_link = f"https://t.me/{Config.BOT_USERNAME}?start=PredatorHackerzZ_{str_to_b64(file_er_id)}"
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


@Bot.on_message(filters.private & filters.command("broadcast") & filters.user(Config.BOT_OWNER) & filters.reply)
async def broadcast_handler_open(_, m: Message):
    await main_broadcast_handler(m, db)


@Bot.on_message(filters.private & filters.command("settings") & filters.user(Config.BOT_OWNER))
async def settings(_, m: Message):
    delay = await db.get_auto_delete_seconds()
    current = "Disabled" if delay <= 0 else f"{delay // 60} minute(s)"
    await m.reply_text(
        "**⚙️ HJ GROUPS BOT SETTINGS**\n\n"
        f"**Auto-delete delivered files:** `{current}`\n\n"
        "Choose how long a file delivered through a start/share link remains in the user's chat.\n"
        "This setting is available to the bot owner only.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("5 min", callback_data="setdel_300"),
             InlineKeyboardButton("15 min", callback_data="setdel_900"),
             InlineKeyboardButton("30 min", callback_data="setdel_1800")],
            [InlineKeyboardButton("1 hour", callback_data="setdel_3600"),
             InlineKeyboardButton("6 hours", callback_data="setdel_21600"),
             InlineKeyboardButton("24 hours", callback_data="setdel_86400")],
            [InlineKeyboardButton("♾️ Disable", callback_data="setdel_0")],
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

    elif cb_data == "refreshForceSub":
        if Config.UPDATES_CHANNEL:
            result = await handle_force_sub(bot, cmd.message, user_id=cmd.from_user.id)
            if result == 400:
                return
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
        if int(cmd.from_user.id) != Config.BOT_OWNER:
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

    elif cb_data == "open_settings":
        if int(cmd.from_user.id) != Config.BOT_OWNER:
            await cmd.answer("You are not allowed to open bot settings.", show_alert=True)
            return
        delay = await db.get_auto_delete_seconds()
        current = "Disabled" if delay <= 0 else f"{delay // 60} minute(s)"
        await cmd.message.edit(
            "**⚙️ HJ GROUPS BOT SETTINGS**\n\n"
            f"**Auto-delete delivered files:** `{current}`\n\n"
            "Choose how long a delivered file remains in the user's chat.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("5 min", callback_data="setdel_300"),
                 InlineKeyboardButton("15 min", callback_data="setdel_900"),
                 InlineKeyboardButton("30 min", callback_data="setdel_1800")],
                [InlineKeyboardButton("1 hour", callback_data="setdel_3600"),
                 InlineKeyboardButton("6 hours", callback_data="setdel_21600"),
                 InlineKeyboardButton("24 hours", callback_data="setdel_86400")],
                [InlineKeyboardButton("♾️ Disable", callback_data="setdel_0")],
                [InlineKeyboardButton("Close", callback_data="closeMessage")]
            ])
        )

    elif cb_data == "closeMessage":
        await cmd.message.delete(True)

    try:
        await cmd.answer()
    except QueryIdInvalid:
        pass


async def setup_bot_commands():
    await Bot.set_bot_commands([
        BotCommand("start", "Start the bot / open file links"),
        BotCommand("clear_batch", "Clear your current batch"),
        BotCommand("status", "Admin: show total users"),
        BotCommand("broadcast", "Admin: broadcast a replied message"),
        BotCommand("ban_user", "Admin: ban a user"),
        BotCommand("unban_user", "Admin: unban a user"),
        BotCommand("banned_users", "Admin: list banned users"),
        BotCommand("settings", "Admin: bot settings"),
    ])


async def run_bot():
    await Bot.start()
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
