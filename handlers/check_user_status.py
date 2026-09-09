# (c) Mr. Vishal & @AbirHasan2005 @PredatorHackerzZ

import datetime
from configs import Config
from handlers.database import db
from pyrogram.errors import PeerIdInvalid


async def handle_user_status(bot, cmd):
    chat_id = cmd.from_user.id
    if not await db.is_user_exist(chat_id):
        await db.add_user(chat_id)
        if Config.LOG_CHANNEL is not None:
            try:
                await bot.send_message(
                    int(Config.LOG_CHANNEL),
                    f"#NEW_USER: \n\nNew User [{cmd.from_user.first_name}](tg://user?id={cmd.from_user.id}) started @{Config.BOT_USERNAME} !!"
                )
            except PeerIdInvalid:
                # An invalid/inaccessible log channel must not break normal bot usage.
                pass

    ban_status = await db.get_ban_status(chat_id)
    if ban_status["is_banned"]:
        if (
                datetime.date.today() - datetime.date.fromisoformat(ban_status["banned_on"])
        ).days > ban_status["ban_duration"]:
            await db.remove_ban(chat_id)
        else:
            await cmd.reply_text("You R Banned!.. Contact @TeleRoid14 😝", quote=True)
            return
    await cmd.continue_propagation()
