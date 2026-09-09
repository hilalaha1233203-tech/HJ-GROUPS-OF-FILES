# HJ GROUPS OF FILES

import os


class Config(object):
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    BOT_USERNAME = os.environ.get("BOT_USERNAME")

    # Optional legacy bootstrap value. Leave empty when using automatic multi-channel detection.
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

📁 Send any supported file or media to save it in the configured Telegram storage channels and receive a permanent shareable link.

🔐 Supabase stores users and bot settings.
☁️ Telegram channels store the actual files.

Supports single-file links, batch links, multiple storage channels, URL shortener, broadcasts and admin controls.
"""

    HOME_TEXT = """
Hello, [{}](tg://user?id={}) 👋

**HJ GROUPS OF FILES**

Permanent Telegram **FileStore Bot**.

📁 Send a file to save it in your configured storage channels and generate a shareable link.

⚡ Fast • Simple • Permanent

Use **Commands** to view all available commands and features.
"""

    COMMANDS_TEXT = """
📚 **Available Commands:**

➜ `/start` — Start the bot / open a share link.
➜ `/genlink` — Store a single replied message or file.
➜ `/batch` — Create a batch link from saved/replied messages.
➜ `/custom_batch` — Create a batch from multiple selected messages or links.
➜ `/shortener` — Shorten a shareable link.
➜ `/settings` — Customize bot settings. Owner only.
➜ `/clear_batch` — Clear your current batch.

🛡️ **Moderators Commands:**

➜ `/special_link` — Create an editable batch/share link.
➜ `/universal_link` — Create a link payload that can be reused by supported clones.
➜ `/broadcast` — Broadcast a message to registered users.
➜ `/ban` — Ban a user.
➜ `/unban` — Unban a user.

**Legacy admin commands**
➜ `/status` — Show total registered users.
➜ `/ban_user` — Ban a user for a number of days.
➜ `/unban_user` — Remove a user ban.
➜ `/banned_users` — List banned users.
"""
