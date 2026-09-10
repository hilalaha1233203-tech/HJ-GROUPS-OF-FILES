# HJ GROUPS OF FILES - user status middleware

import datetime
from configs import Config
from handlers.database import db


async def handle_user_status(bot, cmd):
    """Register/check a user without ever blocking normal command handlers.

    Older deployments may have a different users-table schema. Database/status
    errors are therefore isolated here so /start, /settings and every command
    can still reach its own handler while the database is being repaired.
    """
    chat_id = int(cmd.from_user.id)

    try:
        if not await db.is_user_exist(chat_id):
            try:
                await db.add_user(chat_id)
            except Exception as err:
                print(f"[USER_DB] Could not add user {chat_id}: {err}")

            if Config.LOG_CHANNEL:
                try:
                    await bot.send_message(
                        int(Config.LOG_CHANNEL),
                        f"#NEW_USER:\n\nNew User [{cmd.from_user.first_name}](tg://user?id={chat_id}) started @{Config.BOT_USERNAME} !!"
                    )
                except Exception:
                    pass

        try:
            ban_status = await db.get_ban_status(chat_id)
        except Exception as err:
            # Never swallow the command because an old users-table schema is
            # missing optional ban columns.
            print(f"[USER_DB] Ban-status check skipped for {chat_id}: {err}")
            ban_status = {"is_banned": False}

        if ban_status.get("is_banned"):
            try:
                banned_on = datetime.date.fromisoformat(ban_status.get("banned_on"))
                duration = int(ban_status.get("ban_duration", 0))
                if (datetime.date.today() - banned_on).days > duration:
                    await db.remove_ban(chat_id)
                else:
                    await cmd.reply_text("You R Banned!.. Contact the bot owner for help.", quote=True)
                    return
            except Exception as err:
                print(f"[USER_DB] Invalid ban record for {chat_id}: {err}")
    except Exception as err:
        print(f"[USER_DB] Middleware error for {chat_id}: {err}")

    await cmd.continue_propagation()
