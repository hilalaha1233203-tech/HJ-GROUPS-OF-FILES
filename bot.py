# HJ GROUPS OF FILES - User-friendly FileStore entrypoint
# The complete legacy implementation lives in bot_legacy.py.
# This wrapper adds the simple forwarding workflow without removing
# the legacy settings, storage, admin, broadcast and link functionality.

import asyncio

import bot_legacy
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, BotCommand

from configs import Config
from handlers.save_media import save_media_in_channel, save_batch_media_in_channel

Bot = bot_legacy.Bot

# Per-user temporary workflow state. The actual files remain in Telegram storage;
# this state only remembers forwarded message IDs until the command is used.
USER_WORKFLOW = {}

FILESTORE_COMMANDS = set(bot_legacy.FILESTORE_COMMANDS)


def _state(user_id: int):
    return USER_WORKFLOW.setdefault(
        str(int(user_id)),
        {"recent": []},
    )


def _get_forward_origin(message):
    """Return (source_chat_id, source_message_id) for a forwarded Telegram message."""
    if message is None:
        return None

    # Newer Pyrogram / Bot API representation.
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None) if origin else None
    origin_message_id = getattr(origin, "message_id", None) if origin else None
    if origin_chat is not None and origin_message_id:
        return int(origin_chat.id), int(origin_message_id)

    # Legacy representation.
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
    """Old/simple UX: forward file first, then just send /genlink."""
    target = m.reply_to_message
    if target is None:
        target = await _find_last_forwarded_message(bot, m)

    if target is None:
        await m.reply_text(
            "📎 First forward the file/message from your channel to this bot.\n\n"
            "Then send `/genlink`."
        )
        return

    status = await m.reply_text("⏳ Generating your permanent link...")
    await save_media_in_channel(bot, status, target)


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

    status = await m.reply_text(
        f"⏳ Creating one batch link for `{total}` messages..."
    )
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
    """Simple batch UX, with the original numeric syntax kept as fallback."""
    # Original compatibility: /batch start_id end_id after a replied source message.
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
        status = await m.reply_text(
            f"⏳ Creating one batch link for `{high - low + 1}` messages..."
        )
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
    """Simple custom batch: forward the selected messages, then send /custom_batch."""
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

    status = await m.reply_text(
        f"⏳ Creating one batch link for `{len(ids)}` selected messages..."
    )
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


# Replace only the command menu text; all legacy command handlers remain available.
async def setup_bot_commands():
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
        BotCommand("ban", "Ban a user"),
        BotCommand("unban", "Unban a user"),
        BotCommand("status", "Show total users"),
        BotCommand("ban_user", "Ban a user for days"),
        BotCommand("unban_user", "Remove a user ban"),
        BotCommand("banned_users", "List banned users"),
    ])


# bot_legacy.run_bot() looks up setup_bot_commands in its own module namespace.
# Point that name at our clearer menu without changing the legacy startup routine.
bot_legacy.setup_bot_commands = setup_bot_commands


# Keep legacy module startup behavior, including DB checks, storage recovery,
# callbacks, settings and idle loop.
run_bot = bot_legacy.run_bot


if __name__ == "__main__":
    Bot.run(run_bot())
