# HJ GROUPS OF FILES - User-friendly FileStore entrypoint
# The complete legacy implementation lives in bot_legacy.py.
# This wrapper adds the simple forwarding workflow and admin UX without removing
# the legacy storage, settings, broadcast and link functionality.

import bot_legacy
from pyrogram import filters
from pyrogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton

from configs import Config
from handlers.database import db
from handlers.save_media import save_media_in_channel, save_batch_media_in_channel

Bot = bot_legacy.Bot

# Per-user temporary workflow state. The actual files remain in Telegram storage;
# this state only remembers forwarded message IDs until the command is used.
USER_WORKFLOW = {}

FILESTORE_COMMANDS = list(bot_legacy.FILESTORE_COMMANDS)


def _state(user_id: int):
    return USER_WORKFLOW.setdefault(str(int(user_id)), {"recent": []})


def _get_forward_origin(message):
    """Return (source_chat_id, source_message_id) for a forwarded Telegram message."""
    if message is None:
        return None

    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None) if origin else None
    origin_message_id = getattr(origin, "message_id", None) if origin else None
    if origin_chat is not None and origin_message_id:
        return int(origin_chat.id), int(origin_message_id)

    forwarded_chat = getattr(message, "forward_from_chat", None)
    forwarded_message_id = getattr(message, "forward_from_message_id", None)
    if forwarded_chat is not None and forwarded_message_id:
        return int(forwarded_chat.id), int(forwarded_message_id)

    return None


def _remember_forward(message):
    origin = _get_forward_origin(message)
    if origin is None or not message.from_user:
        return None

    state = _state(message.from_user.id)
    item = {
        "source_chat_id": int(origin[0]),
        "source_message_id": int(origin[1]),
        "bot_message_id": int(message.id),
    }

    state["recent"] = [
        x for x in state["recent"]
        if not (
            int(x["source_chat_id"]) == int(item["source_chat_id"])
            and int(x["source_message_id"]) == int(item["source_message_id"])
        )
    ][-99:]
    state["recent"].append(item)
    return item


def _latest_source_items(user_id: int):
    recent = _state(user_id)["recent"]
    if not recent:
        return []
    source_chat_id = int(recent[-1]["source_chat_id"])
    return [x for x in recent if int(x["source_chat_id"]) == source_chat_id]


def _clear_state(user_id: int):
    USER_WORKFLOW[str(int(user_id))] = {"recent": []}


@Bot.on_message(
    filters.private
    & (filters.document | filters.video | filters.audio | filters.photo | filters.animation)
    & ~filters.command(FILESTORE_COMMANDS),
    group=-1,
)
async def remember_forwarded_media(_, message: Message):
    _remember_forward(message)


@Bot.on_message(
    filters.private & filters.text & ~filters.command(FILESTORE_COMMANDS),
    group=-1,
)
async def remember_forwarded_text(_, message: Message):
    _remember_forward(message)


async def _find_last_forwarded_message(bot, m):
    recent = _state(m.from_user.id)["recent"]
    if not recent:
        return None
    try:
        return await bot.get_messages(m.chat.id, recent[-1]["bot_message_id"])
    except Exception:
        return None


@Bot.on_message(filters.private & filters.command("genlink"), group=-1)
async def user_genlink(bot, m: Message):
    """Forward file first, then send /genlink."""
    target = m.reply_to_message or await _find_last_forwarded_message(bot, m)
    if target is None:
        await m.reply_text(
            "📎 First forward the file/message from your channel to this bot.\n\n"
            "Then send `/genlink`."
        )
        return

    status = await m.reply_text("⏳ Generating your permanent link...")
    await save_media_in_channel(bot, status, target)
    _clear_state(m.from_user.id)


async def _make_batch_link(bot, m, items):
    if len(items) < 2:
        await m.reply_text(
            "📦 To create one batch link:\n\n"
            "1️⃣ Forward the FIRST message from your channel.\n"
            "2️⃣ Forward the LAST message from the same channel.\n"
            "3️⃣ Send `/batch`."
        )
        return

    first = items[-2]
    last = items[-1]
    if int(first["source_chat_id"]) != int(last["source_chat_id"]):
        await m.reply_text("❌ FIRST and LAST messages must be from the same channel.")
        return

    start_id = int(first["source_message_id"])
    end_id = int(last["source_message_id"])
    low, high = sorted((start_id, end_id))
    total = high - low + 1

    status = await m.reply_text(f"⏳ Creating one batch link for `{total}` messages...")
    await save_batch_media_in_channel(
        bot=bot,
        editable=status,
        message_ids=list(range(low, high + 1)),
        source_chat_id=int(first["source_chat_id"]),
        request_user_id=int(m.from_user.id),
    )
    _clear_state(m.from_user.id)


@Bot.on_message(filters.private & filters.command("batch"), group=-1)
async def user_batch(bot, m: Message):
    # Original numeric syntax remains supported for compatibility.
    if len(m.command) >= 3:
        try:
            start_id = int(m.command[1])
            end_id = int(m.command[2])
        except ValueError:
            await m.reply_text("❌ Message IDs must be numbers.")
            return

        origin = _get_forward_origin(m.reply_to_message) if m.reply_to_message else None
        recent = _state(m.from_user.id)["recent"]
        source_chat_id = (
            origin[0]
            if origin is not None
            else (int(recent[-1]["source_chat_id"]) if recent else None)
        )
        if source_chat_id is None:
            await m.reply_text("📎 Forward one message from the source channel first.")
            return

        low, high = sorted((start_id, end_id))
        status = await m.reply_text(f"⏳ Creating one batch link for `{high - low + 1}` messages...")
        await save_batch_media_in_channel(
            bot=bot,
            editable=status,
            message_ids=list(range(low, high + 1)),
            source_chat_id=int(source_chat_id),
            request_user_id=int(m.from_user.id),
        )
        _clear_state(m.from_user.id)
        return

    await _make_batch_link(bot, m, _latest_source_items(m.from_user.id))


@Bot.on_message(filters.private & filters.command("custom_batch"), group=-1)
async def user_custom_batch(bot, m: Message):
    origin = _get_forward_origin(m.reply_to_message) if m.reply_to_message else None

    if len(m.command) >= 2:
        try:
            ids = [int(x) for x in m.command[1:]]
        except ValueError:
            await m.reply_text("❌ Message IDs must be numbers.")
            return

        recent = _state(m.from_user.id)["recent"]
        source_chat_id = (
            origin[0]
            if origin is not None
            else (int(recent[-1]["source_chat_id"]) if recent else None)
        )
        if source_chat_id is None:
            await m.reply_text("📎 Forward one message from the source channel first.")
            return
    else:
        selected = _latest_source_items(m.from_user.id)
        if not selected:
            await m.reply_text(
                "📦 Forward the messages you want in the batch, then send `/custom_batch`."
            )
            return
        source_chat_id = int(selected[-1]["source_chat_id"])
        ids = []
        for item in selected:
            mid = int(item["source_message_id"])
            if mid not in ids:
                ids.append(mid)

    status = await m.reply_text(f"⏳ Creating one batch link for `{len(ids)}` selected messages...")
    await save_batch_media_in_channel(
        bot=bot,
        editable=status,
        message_ids=ids,
        source_chat_id=int(source_chat_id),
        request_user_id=int(m.from_user.id),
    )
    _clear_state(m.from_user.id)


@Bot.on_message(filters.private & filters.command("special_link"), group=-1)
async def user_special_link(bot, m: Message):
    await _make_batch_link(bot, m, _latest_source_items(m.from_user.id))


@Bot.on_message(filters.private & filters.command("universal_link"), group=-1)
async def user_universal_link(bot, m: Message):
    await _make_batch_link(bot, m, _latest_source_items(m.from_user.id))


# Hide the old /ban and /unban aliases. Keep /ban_user and /unban_user as the
# only visible/usable admin ban commands.
@Bot.on_message(filters.private & filters.command("ban"), group=-1)
async def removed_ban_alias(_, m: Message):
    await m.reply_text("🚫 `/ban` has been removed. Use `/ban_user @username days reason`.")


@Bot.on_message(filters.private & filters.command("unban"), group=-1)
async def removed_unban_alias(_, m: Message):
    await m.reply_text("🚫 `/unban` has been removed. Use `/unban_user @username`.")


def _admin_only(user_id: int) -> bool:
    return bool(Config.BOT_OWNER) and int(user_id) == int(Config.BOT_OWNER)


async def _user_display(bot, user_id: int):
    """Best-effort Telegram profile lookup for the admin Users list."""
    try:
        user = await bot.get_users(int(user_id))
        username = getattr(user, "username", None)
        first = getattr(user, "first_name", None) or ""
        last = getattr(user, "last_name", None) or ""
        name = " ".join(x for x in (first, last) if x).strip() or "Unknown User"
        return (f"@{username}" if username else name), username
    except Exception:
        return "Unknown User", None


async def _render_admin_users(bot, message, page=0):
    users = []
    async for row in await db.get_all_users():
        users.append(row)

    per_page = 8
    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    current = users[start:start + per_page]

    lines = [f"👥 **Registered Users** — `{len(users)}` total", ""]
    buttons = []
    for row in current:
        user_id = int(row["id"])
        label, username = await _user_display(bot, user_id)
        status = "🚫 BANNED" if row.get("ban_status", {}).get("is_banned") else "✅"
        if username:
            lines.append(f"{status} `{label}` — `{user_id}`")
        else:
            lines.append(f"{status} **{label}** — `{user_id}`")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"admin_users_{page-1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Status", callback_data="admin_status")])

    await message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Bot.on_message(filters.private & filters.command("status"), group=-1)
async def admin_status(bot, m: Message):
    if not _admin_only(m.from_user.id):
        await m.reply_text("⛔ Owner/Admin Only")
        return
    total_users = await db.total_users_count()
    await m.reply_text(
        f"📊 **Bot Status**\n\n👥 **Total Users:** `{total_users}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 See Users", callback_data="admin_users_0")],
            [InlineKeyboardButton("🚫 Banned Users", callback_data="admin_banned")],
        ]),
    )


@Bot.on_callback_query(filters.regex(r"^admin_users_\d+$"), group=-1)
async def admin_users_callback(bot, query):
    if not _admin_only(query.from_user.id):
        await query.answer("Owner/Admin Only", show_alert=True)
        return
    page = int(query.data.rsplit("_", 1)[1])
    await query.answer()
    await _render_admin_users(bot, query.message, page)


@Bot.on_callback_query(filters.regex(r"^admin_status$"), group=-1)
async def admin_status_callback(bot, query):
    if not _admin_only(query.from_user.id):
        await query.answer("Owner/Admin Only", show_alert=True)
        return
    total_users = await db.total_users_count()
    await query.answer()
    await query.message.edit_text(
        f"📊 **Bot Status**\n\n👥 **Total Users:** `{total_users}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 See Users", callback_data="admin_users_0")],
            [InlineKeyboardButton("🚫 Banned Users", callback_data="admin_banned")],
        ]),
    )


@Bot.on_callback_query(filters.regex(r"^admin_banned$"), group=-1)
async def admin_banned_callback(bot, query):
    if not _admin_only(query.from_user.id):
        await query.answer("Owner/Admin Only", show_alert=True)
        return

    rows = []
    async for row in await db.get_all_banned_users():
        rows.append(row)

    lines = [f"🚫 **Banned Users** — `{len(rows)}` total", ""]
    for row in rows[:50]:
        user_id = int(row["id"])
        label, username = await _user_display(bot, user_id)
        ban = row.get("ban_status", {})
        who = f"@{username}" if username else label
        lines.append(
            f"• **{who}** — `{user_id}`\n"
            f"  Days: `{ban.get('ban_duration', 0)}` | Reason: `{ban.get('ban_reason', '')}`"
        )

    await query.answer()
    await query.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Status", callback_data="admin_status")]
        ]),
    )


async def _resolve_user_id(bot, value: str):
    """Resolve a numeric ID or @username to a Telegram user ID."""
    value = str(value or "").strip()
    if value.lstrip("-").isdigit():
        return int(value)

    username = value.lstrip("@").strip()
    if not username:
        raise ValueError("Invalid username")

    # Fast path: Pyrogram can resolve public usernames directly when available.
    try:
        user = await bot.get_users(username)
        if user and getattr(user, "id", None):
            return int(user.id)
    except Exception:
        pass

    # Fallback: scan registered users and compare their current Telegram username.
    async for row in await db.get_all_users():
        user_id = int(row["id"])
        try:
            user = await bot.get_users(user_id)
            current = (getattr(user, "username", None) or "").lower()
            if current == username.lower():
                return user_id
        except Exception:
            continue

    raise ValueError(f"User `{value}` was not found in the registered users.")


@Bot.on_message(filters.private & filters.command("ban_user"), group=-1)
async def admin_ban_user(bot, m: Message):
    if not _admin_only(m.from_user.id):
        await m.reply_text("⛔ Owner/Admin Only")
        return
    if len(m.command) < 4:
        await m.reply_text(
            "Usage: `/ban_user @username days reason`\n\n"
            "Example: `/ban_user @someuser 28 You misused the bot.`"
        )
        return

    try:
        user_id = await _resolve_user_id(bot, m.command[1])
        ban_duration = int(m.command[2])
        if ban_duration < 0:
            raise ValueError("Ban duration cannot be negative")
        ban_reason = " ".join(m.command[3:]).strip()
        if not ban_reason:
            raise ValueError("Ban reason is required")

        await db.ban_user(user_id, ban_duration, ban_reason)
        try:
            await bot.send_message(
                user_id,
                f"You are banned from using this bot for **{ban_duration}** day(s).\n\nReason: __{ban_reason}__"
            )
        except Exception:
            pass
        await m.reply_text(f"✅ User `{user_id}` banned for `{ban_duration}` day(s).\nReason: `{ban_reason}`")
    except Exception as err:
        await m.reply_text(f"❌ Could not ban user.\n\n`{err}`")


@Bot.on_message(filters.private & filters.command("unban_user"), group=-1)
async def admin_unban_user(bot, m: Message):
    if not _admin_only(m.from_user.id):
        await m.reply_text("⛔ Owner/Admin Only")
        return
    if len(m.command) < 2:
        await m.reply_text("Usage: `/unban_user @username`")
        return

    try:
        user_id = await _resolve_user_id(bot, m.command[1])
        await db.remove_ban(user_id)
        try:
            await bot.send_message(user_id, "✅ Your bot ban has been lifted.")
        except Exception:
            pass
        await m.reply_text(f"✅ User `{user_id}` unbanned.")
    except Exception as err:
        await m.reply_text(f"❌ Could not unban user.\n\n`{err}`")


@Bot.on_message(filters.private & filters.command("clear_batch"), group=-1)
async def clear_user_batch_wrapper(_, m: Message):
    _clear_state(m.from_user.id)
    try:
        bot_legacy.MediaList[str(m.from_user.id)] = []
    except Exception:
        pass
    await m.reply_text("✅ Cleared your batch selection successfully!")


async def setup_bot_commands():
    # /ban and /unban intentionally removed from the command menu.
    await Bot.set_bot_commands([
        BotCommand("start", "Start the bot / open file links"),
        BotCommand("genlink", "Forward a file, then send /genlink"),
        BotCommand("batch", "Forward first + last message, then /batch"),
        BotCommand("custom_batch", "Forward selected messages, then /custom_batch"),
        BotCommand("shortener", "Shorten a shareable link"),
        BotCommand("settings", "Customize bot settings"),
        BotCommand("clear_batch", "Clear your forwarded batch selection"),
        BotCommand("special_link", "Create one batch link from forwarded messages"),
        BotCommand("universal_link", "Create one reusable batch link"),
        BotCommand("broadcast", "Broadcast a replied message"),
        BotCommand("status", "Show total users and user list"),
        BotCommand("ban_user", "Ban a user by @username"),
        BotCommand("unban_user", "Unban a user by @username"),
        BotCommand("banned_users", "List banned users"),
    ])


# bot_legacy.run_bot() looks up setup_bot_commands in its own module namespace.
bot_legacy.setup_bot_commands = setup_bot_commands
run_bot = bot_legacy.run_bot


if __name__ == "__main__":
    Bot.run(run_bot())
