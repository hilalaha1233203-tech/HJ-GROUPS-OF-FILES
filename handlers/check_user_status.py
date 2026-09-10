# HJ GROUPS OF FILES - user status middleware

import asyncio
import datetime
from configs import Config
from handlers.database import db

DB_TIMEOUT = 6


async def _safe_db(call, label):
    try:
        return await asyncio.wait_for(call, timeout=DB_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[USER_DB] {label} timed out after {DB_TIMEOUT}s; continuing without blocking command handling")
    except Exception as err:
        print(f"[USER_DB] {label} failed: {err}")
    return None


async def handle_user_status(bot, cmd):
    """Register/check a user without allowing a slow DB/network call to stall the bot."""
    chat_id = int(cmd.from_user.id)

    exists = await _safe_db(db.is_user_exist(chat_id), f"existence check for {chat_id}")
    if exists is False:
        added = await _safe_db(db.add_user(chat_id), f"insert user {chat_id}")
        if added is not None and Config.LOG_CHANNEL:
            try:
                await bot.send_message(
                    int(Config.LOG_CHANNEL),
                    f"#NEW_USER:\n\nNew User [{cmd.from_user.first_name}](tg://user?id={chat_id}) started @{Config.BOT_USERNAME} !!"
                )
            except Exception as err:
                print(f"[USER_DB] New-user log failed for {chat_id}: {err}")

    ban_status = await _safe_db(db.get_ban_status(chat_id), f"ban-status check for {chat_id}")
    if ban_status and ban_status.get("is_banned"):
        try:
            banned_on = datetime.date.fromisoformat(ban_status.get("banned_on"))
            duration = int(ban_status.get("ban_duration", 0))
            if (datetime.date.today() - banned_on).days > duration:
                await _safe_db(db.remove_ban(chat_id), f"ban cleanup for {chat_id}")
            else:
                await cmd.reply_text("You R Banned!.. Contact the bot owner for help.", quote=True)
                return
        except Exception as err:
            print(f"[USER_DB] Invalid ban record for {chat_id}: {err}")

    await cmd.continue_propagation()
