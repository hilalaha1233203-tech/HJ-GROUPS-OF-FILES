# HJ GROUPS OF FILES

import os


class Config(object):
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    BOT_USERNAME = os.environ.get("BOT_USERNAME")
    # Optional legacy bootstrap. Automatic multi-channel detection is preferred.
    DB_CHANNEL = os.environ.get("DB_CHANNEL")
    if DB_CHANNEL:
        DB_CHANNEL = int(DB_CHANNEL)

    SHORTLINK_URL = os.environ.get("SHORTLINK_URL")
    SHORTLINK_API = os.environ.get("SHORTLINK_API")
    BOT_OWNER = int(os.environ.get("BOT_OWNER", "0"))

    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = (
        os.environ.get("SUPABASE_SECRET_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    DATABASE_URL = SUPABASE_URL

    # Force Subscribe is intentionally disabled.
    UPDATES_CHANNEL = None
    LOG_CHANNEL = os.environ.get("LOG_CHANNEL") or None

    # Delivery caption / join buttons. Use @username or a full https://t.me/... URL.
    MAIN_CHANNEL = os.environ.get("MAIN_CHANNEL") or "@hjgroups_1"
    BACKUP_CHANNEL = os.environ.get("BACKUP_CHANNEL") or None
    POCKET_LIBRARY = os.environ.get("POCKET_LIBRARY") or None
    DELIVERY_TAG = "@hjgroups_1"

    OTHER_USERS_CAN_SAVE_FILE = [
        int(user_id)
        for user_id in os.environ.get("OTHER_USERS_CAN_SAVE_FILE", "").split(",")
        if user_id.strip()
    ]

    BANNED_USERS = set(
        int(x) for x in os.environ.get("BANNED_USERS", "").split() if x.strip()
    )
    FORWARD_AS_COPY = os.environ.get("FORWARD_AS_COPY", "True").lower() == "true"
    BROADCAST_AS_COPY = os.environ.get("BROADCAST_AS_COPY", "False").lower() == "true"
    BANNED_CHAT_IDS = list(
        set(
            int(x)
            for x in os.environ.get("BANNED_CHAT_IDS", "").split()
            if x.strip()
        )
    )

    ABOUT_BOT_TEXT = """
**HJ GROUPS OF FILES**

Permanent Telegram FileStore Bot.

📁 Send any supported file or media to save it in a Telegram storage channel and receive a permanent shareable link.

🔐 Supabase stores users and bot settings.
☁️ Telegram channels store the actual files.

Supports single-file links, batch links, multiple storage channels, URL shortener, broadcasts and admin controls.
"""

    HOME_TEXT = """
Hello, [{}](tg://user?id={}) 👋

**HJ GROUPS OF FILES**

Permanent Telegram **FileStore Bot**.

📁 Send a file to save it in your configured storage channel(s) and generate a shareable link.

⚡ Fast • Simple • Permanent

Use **Commands** to view all available commands and features.
"""

    COMMANDS_TEXT = """
📚 **Available Commands:**

➜ `/start` — Start the bot / open a share link.
➜ `/genlink` — Forward a file/message, then send this command.
➜ `/batch` — Forward the FIRST and LAST message, then send this command.
➜ `/custom_batch` — Forward the messages you want, then send this command.
➜ `/shortener` — Shorten any shareable link.
➜ `/settings` — Customize your settings as your need.
➜ `/clear_batch` — Clear your current batch selection.

🛡️ **Moderator Commands:**

➜ `/special_link` — Create a batch link using your forwarded selection.
➜ `/universal_link` — Create a batch link using your forwarded selection.
➜ `/broadcast` — Broadcast a message to users.

**Admin Commands**
➜ `/status` — Show total registered users and open the user list.
➜ `/ban_user` — Ban a user by `@username` for a number of days.
➜ `/unban_user` — Remove a user ban by `@username`.
➜ `/banned_users` — List banned users.
"""
