# HJ GROUPS OF FILES

import os


class Config(object):
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    BOT_USERNAME = os.environ.get("BOT_USERNAME")
    DB_CHANNEL = int(os.environ.get("DB_CHANNEL", "-100"))
    SHORTLINK_URL = os.environ.get("SHORTLINK_URL")
    SHORTLINK_API = os.environ.get("SHORTLINK_API")
    BOT_OWNER = int(os.environ.get("BOT_OWNER", "0"))

    # Supabase replaces MongoDB for user/status data.
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = (
        os.environ.get("SUPABASE_SECRET_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
    )
    # Kept only as a compatibility alias for any older imported code.
    DATABASE_URL = SUPABASE_URL

    # Optional channel settings. Blank means disabled.
    UPDATES_CHANNEL = os.environ.get("UPDATES_CHANNEL") or None
    LOG_CHANNEL = os.environ.get("LOG_CHANNEL") or None

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
    OTHER_USERS_CAN_SAVE_FILE = [
        int(user_id)
        for user_id in os.environ.get("OTHER_USERS_CAN_SAVE_FILE", "").split(",")
        if user_id.strip()
    ]

    ABOUT_BOT_TEXT = f"""
**HJ GROUPS OF FILES**

Permanent Telegram File Store Bot.

📁 Send any supported file or media to save it in the configured private Telegram database channel and receive a shareable link.

🔐 Supabase is used only for user/status records.
☁️ Actual files remain stored in the configured Telegram DB channel.

Use the Commands button below to see the available bot commands.
"""

    ABOUT_DEV_TEXT = """
**HJ GROUPS OF FILES**

This bot is maintained for HJ GROUPS.

The original TG-FileStore functionality is preserved, with Supabase used as the user database and Voroa used for hosting.
"""

    HOME_TEXT = """
Hello, [{}](tg://user?id={}) 👋

**HJ GROUPS OF FILES**

This is a permanent Telegram **FileStore Bot**.

📁 Send me any file or media and I will save it to the configured private Telegram storage channel and generate a shareable link.

⚡ Fast • Simple • Permanent

Use **Commands** to view the available bot commands.
"""
