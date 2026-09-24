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

🛠️ **Owner Maintenance Commands**
➜ `/caption_replace` — Select an admin channel and exact-replace text inside its media captions. Private channels can be added by forwarding one message.
➜ `/set_caption` — Select an admin channel and set a caption across all or a message-ID range.

🛡️ **Moderator Commands:**
