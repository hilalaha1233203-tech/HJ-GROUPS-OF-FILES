"""Owner-only bulk caption maintenance tools.

Caption Replace performs exact FIND -> REPLACE inside media captions.
Set Caption replaces the complete caption on selected media messages.

Targets are maintained in a persistent bot-compatible channel registry and are
revalidated for administrator/edit permission before use. The existing FileStore
and storage architecture is not changed.
"""
import asyncio
import copy
import html
import json
import os
import re
import tempfile
import time

from pyrogram import enums, filters, StopPropagation
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import bot_legacy
from configs import Config
from handlers.database import db
from handlers import send_file
from handlers.telegram_api import (
    get_chat as api_get_chat,
    get_chat_member as api_get_chat_member,
    get_me as api_get_me,
)

Bot = bot_legacy.Bot
_SESSIONS = {}
_RUNTIME = None
_JOB_TASK = None
_PERSIST_LOCK = asyncio.Lock()


def _env_int(name, default, low, high):
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _env_float(name, default):
    try:
        value = float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(0.0, value)


WORKERS = _env_int("CAPTION_REPLACE_WORKERS", 2, 2, 3)
FETCH_BATCH = _env_int("CAPTION_REPLACE_FETCH_BATCH", 100, 1, 200)
CHECKPOINT_EVERY = _env_int("CAPTION_REPLACE_CHECKPOINT_EVERY", 10, 1, 1000)
EDIT_DELAY = _env_float("CAPTION_REPLACE_EDIT_DELAY", 0.15)

_CHANNELS_PER_PAGE = 8
_MAX_RANGE = 200000
_STATUS_EVERY = 25
_CHANNEL_REGISTRY_KEY = "caption_admin_channels"
_MAX_KNOWN_CHANNELS = 500
_RUNTIME_CHANNEL_SEEN = set()


def _owner(user_id):
    return bool(Config.BOT_OWNER) and int(user_id) == int(Config.BOT_OWNER)


def _caption_units(text):
    return len(str(text).encode("utf-16-le")) // 2


def _link(chat_id, message_id, username=None):
    if username:
        return f"https://t.me/{username.lstrip('@')}/{int(message_id)}"
    value = str(abs(int(chat_id)))
    if value.startswith("100"):
        return f"https://t.me/c/{value[3:]}/{int(message_id)}"
    return None


def _u16_index(text, units):
    target = max(0, int(units))
    used = 0
    value = str(text)
    for index, char in enumerate(value):
        if used >= target:
            return index
        used += 2 if ord(char) > 0xFFFF else 1
    return len(value)


def _spans(text, find_text, replace_text):
    result = []
    position = 0
    while True:
        position = text.find(find_text, position)
        if position < 0:
            return result
        end = position + len(find_text)
        result.append((position, end, len(replace_text), end - position))
        position = end


def replace_caption_exact(caption, find_text, replace_text):
    if not caption or not find_text or find_text not in caption:
        return caption, 0
    count = caption.count(find_text)
    return caption.replace(find_text, replace_text), count


def _map_index(position, spans):
    mapped = int(position)
    for start, end, new_length, old_length in spans:
        if position <= start:
            break
        if position >= end:
            mapped += new_length - old_length
        else:
            return start + new_length
    return mapped


def adjust_caption_entities(old_caption, new_caption, entities, find_text, replace_text):
    if entities is None:
        return None
    if not find_text or old_caption == new_caption:
        return copy.deepcopy(list(entities))

    spans = _spans(old_caption, find_text, replace_text)
    result = []
    for entity in entities:
        try:
            offset = int(getattr(entity, "offset", 0))
            length = int(getattr(entity, "length", 0))
            start = _u16_index(old_caption, offset)
            end = _u16_index(old_caption, offset + length)
            overlaps = [
                span for span in spans
                if max(start, span[0]) < min(end, span[1])
            ]

            if any(not (span[0] >= start and span[1] <= end) for span in overlaps):
                continue

            new_start = _map_index(start, spans)
            new_end = _map_index(end, spans)
            if new_end < new_start:
                continue

            adjusted = copy.deepcopy(entity)
            adjusted.offset = _caption_units(new_caption[:new_start])
            adjusted.length = _caption_units(new_caption[new_start:new_end])
            result.append(adjusted)
        except Exception:
            continue
    return result


async def _flood(operation, label):
    while True:
        try:
            return await operation()
        except FloodWait as exc:
            wait_seconds = max(1, int(getattr(exc, "value", 1)))
            print(
                f"[CAPTION_MAINTENANCE] FloodWait {label}: "
                f"waiting {wait_seconds}s"
            )
            await asyncio.sleep(wait_seconds)


async def _get_job():
    raw = await db._get_setting("caption_maintenance_job", "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception as exc:
        print(f"[CAPTION_MAINTENANCE] Invalid saved job: {exc}")
        return None

    if not isinstance(value, dict):
        return None

    required = {"status", "operation", "chat_id", "start_id", "end_id"}
    if not required.issubset(value):
        print("[CAPTION_MAINTENANCE] Ignoring incomplete saved job.")
        return None

    if value.get("operation") == "replace":
        required.update({"find", "replace"})
    elif value.get("operation") == "set":
        required.add("set_caption")
    else:
        print("[CAPTION_MAINTENANCE] Ignoring unknown saved operation.")
        return None

    if not required.issubset(value):
        print("[CAPTION_MAINTENANCE] Ignoring malformed saved job.")
        return None

    value.setdefault("run_mode", "normal")
    value.setdefault("done_ids", [])
    value.setdefault("failed_ids", [])
    value.setdefault("skipped_ids", [])
    value.setdefault("changed", 0)
    value.setdefault("skipped", 0)
    value.setdefault("last_error", "")
    value.setdefault("fatal_error", "")
    value.setdefault("target_username", "")
    value.setdefault("target_title", "")
    return value


async def _set_job(job):
    job["updated_at"] = int(time.time())
    try:
        async with _PERSIST_LOCK:
            await db._set_setting(
                "caption_maintenance_job",
                json.dumps(job, ensure_ascii=False, separators=(",", ":")),
            )
    except Exception as exc:
        print(f"[CAPTION_MAINTENANCE] checkpoint failed: {exc}")


def _total(job):
    return abs(int(job["end_id"]) - int(job["start_id"])) + 1


def _progress(job):
    if str(job.get("run_mode", "normal")).lower() == "retry":
        return len(job.get("done_ids") or []), int(job.get("retry_total", 0))
    return len(job.get("done_ids") or []), _total(job)


def _status_text(job):
    processed, total = _progress(job)
    operation = (
        "Caption Replace"
        if job.get("operation") == "replace"
        else "Set Caption"
    )
    run_mode = (
        "Retry Failed IDs"
        if str(job.get("run_mode")) == "retry"
        else operation
    )

    lines = [
        f"**{operation}**",
        f"Status: {str(job.get('status', 'unknown')).upper()} | Mode: {run_mode}",
        f"Target: {job.get('target_title') or job.get('chat_id')}",
        f"Channel ID: {job.get('chat_id')}",
        f"Range: {job.get('start_id')} -> {job.get('end_id')}",
        f"Progress: {processed} / {total} processed",
        f"Changed: {job.get('changed', 0)}",
        f"Skipped: {job.get('skipped', 0)}",
        f"Failed: {len(job.get('failed_ids') or [])}",
    ]

    if job.get("dry_run"):
        lines.append("Dry run: NO Telegram captions were edited.")

    if job.get("operation") == "replace":
        lines.append(f"FIND: {str(job.get('find', '')).replace(chr(96), chr(180))}")
        lines.append(f"REPLACE: {str(job.get('replace', '')).replace(chr(96), chr(180))}")
    else:
        lines.append(
            f"SET CAPTION: {str(job.get('set_caption', '')).replace(chr(96), chr(180))}"
        )

    if job.get("last_error"):
        lines.append(f"Last error: {str(job['last_error'])[:700]}")
    if job.get("fatal_error"):
        lines.append(f"Job error: {str(job['fatal_error'])[:700]}")
    return "\n".join(lines)


def _keyboard(job, active=False):
    state = str(job.get("status", "")).lower()
    rows = []

    if state == "running":
        rows.append([
            InlineKeyboardButton(
                "Pause" if active else "Resume",
                callback_data="cap:pause" if active else "cap:resume",
            )
        ])
        rows.append([InlineKeyboardButton("Stop", callback_data="cap:stop")])
    elif state in {"paused", "stopped"}:
        rows.append([InlineKeyboardButton("Resume", callback_data="cap:resume")])
        rows.append([InlineKeyboardButton("Stop", callback_data="cap:stop")])
    elif state == "failed":
        rows.append([InlineKeyboardButton("Resume", callback_data="cap:resume")])

    if state in {"stopped", "failed", "completed"} and job.get("failed_ids"):
        rows.append([
            InlineKeyboardButton("Retry Failed IDs", callback_data="cap:retry")
        ])

    if state in {"stopped", "failed", "completed"}:
        rows.append([InlineKeyboardButton("New Job", callback_data="cap:new")])

    rows.append([InlineKeyboardButton("Refresh Status", callback_data="cap:status")])
    return InlineKeyboardMarkup(rows)


def _channel_label(item):
    title = " ".join(str(item.get("title") or "Telegram Channel").split())
    if len(title) > 30:
        title = title[:29] + "…"
    storage_mark = " [HJ STORAGE]" if item.get("is_storage") else ""
    username = f" @{item['username']}" if item.get("username") else ""
    return f"{title}{username}{storage_mark}"[:62]


async def _get_known_channels():
    raw = await db._get_setting(_CHANNEL_REGISTRY_KEY, "[]")
    try:
        value = json.loads(raw) if raw else []
    except Exception:
        return []

    if not isinstance(value, list):
        return []

    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            chat_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if chat_id in seen:
            continue
        seen.add(chat_id)
        result.append({
            "id": chat_id,
            "title": str(item.get("title") or "Telegram Channel"),
            "username": str(item.get("username") or "").lstrip("@"),
        })
    return result


async def _remember_channel(chat):
    if chat is None or getattr(chat, "type", None) != enums.ChatType.CHANNEL:
        return

    try:
        chat_id = int(chat.id)
    except (TypeError, ValueError):
        return

    current = await _get_known_channels()
    item = {
        "id": chat_id,
        "title": str(getattr(chat, "title", "") or "Telegram Channel"),
        "username": str(getattr(chat, "username", "") or "").lstrip("@"),
    }

    updated = []
    replaced = False
    for existing in current:
        if int(existing["id"]) == chat_id:
            updated.append(item)
            replaced = True
        else:
            updated.append(existing)

    if not replaced:
        updated.insert(0, item)

    updated = updated[:_MAX_KNOWN_CHANNELS]
    try:
        await db._set_setting(
            _CHANNEL_REGISTRY_KEY,
            json.dumps(updated, ensure_ascii=False, separators=(",", ":")),
        )
    except Exception as exc:
        print(f"[CAPTION_MAINTENANCE] channel registry save failed: {exc}")


def _forward_source(message):
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin else None
    message_id = getattr(origin, "message_id", None) if origin else None
    if chat is not None and message_id:
        return chat

    legacy_chat = getattr(message, "forward_from_chat", None)
    if legacy_chat is not None:
        return legacy_chat
    return None


def _channel_input_ref(value):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(
            "Send a channel @username, t.me link, numeric channel ID, "
            "or forward one channel message."
        )

    lowered = raw.lower()
    if (
        lowered.startswith("https://t.me/")
        or lowered.startswith("http://t.me/")
        or lowered.startswith("https://telegram.me/")
        or lowered.startswith("http://telegram.me/")
    ):
        tail = raw.split("/", 3)[-1]
        username = tail.split("/", 1)[0]
        if username and username.lower() not in {"c", "joinchat", "+", "s"}:
            return "@" + username.lstrip("@")
        raise ValueError(
            "Private invite links cannot be resolved automatically. "
            "Forward one message from that channel instead."
        )

    if raw.startswith("@"):
        return raw.split()[0]

    if re.fullmatch(r"-?\d+", raw):
        return int(raw.split()[0])

    if re.fullmatch(r"[A-Za-z0-9_]{5,}", raw):
        return "@" + raw

    raise ValueError(
        "Invalid channel reference. Send @username, t.me/username, numeric "
        "channel ID, or forward a channel message."
    )


async def _discover_admin_channels():
    try:
        storage_ids = {int(x) for x in await db.get_storage_channels()}
    except Exception as exc:
        print(f"[CAPTION_MAINTENANCE] Storage list read failed: {exc}")
        storage_ids = set()

    known = await _get_known_channels()
    by_id = {int(item["id"]): item for item in known}

    for chat_id in storage_ids:
        by_id.setdefault(
            chat_id,
            {
                "id": chat_id,
                "title": "HJ Storage Channel",
                "username": "",
            },
        )

    try:
        me = await _flood(api_get_me, "get bot identity")
        bot_id = int((me or {}).get("id") or 0)
        if bot_id <= 0:
            raise ValueError("Telegram Bot API did not return the bot user ID.")
    except Exception as exc:
        raise RuntimeError(
            f"Cannot verify admin channels through Telegram Bot API: {exc}"
        ) from exc

    channels = []
    refreshed = []

    for item in by_id.values():
        chat_id = int(item["id"])
        username = str(item.get("username") or "").lstrip("@")
        ref = "@" + username if username else chat_id

        try:
            chat_data = await _flood(
                lambda target=ref: api_get_chat(target),
                f"channel metadata {ref}",
            )
        except Exception as exc:
            print(
                f"[CAPTION_MAINTENANCE] Channel {chat_id} could not be "
                f"resolved by Bot API: {exc}"
            )
            continue

        chat_type = str((chat_data or {}).get("type") or "")
        if chat_type != "channel":
            continue

        canonical_id = int((chat_data or {}).get("id") or chat_id)
        try:
            member = await _flood(
                lambda cid=canonical_id: api_get_chat_member(cid, bot_id),
                f"admin check {canonical_id}",
            )
        except Exception as exc:
            print(
                f"[CAPTION_MAINTENANCE] Cannot inspect admin rights for "
                f"{canonical_id} via Bot API: {exc}"
            )
            continue

        status = str((member or {}).get("status") or "").lower()
        is_creator = status in {"creator", "owner"}
        is_admin = is_creator or status == "administrator"
        if not is_admin:
            continue

        if not is_creator and status == "administrator":
            can_edit = (member or {}).get("can_edit_messages")
            if can_edit is False:
                continue

        title = str(
            (chat_data or {}).get("title")
            or item.get("title")
            or "Telegram Channel"
        )
        current_username = str(
            (chat_data or {}).get("username")
            or username
            or ""
        ).lstrip("@")

        channel_item = {
            "id": canonical_id,
            "title": title,
            "username": current_username,
            "is_storage": canonical_id in storage_ids,
        }
        channels.append(channel_item)
        refreshed.append({
            "id": canonical_id,
            "title": title,
            "username": current_username,
        })

    old_by_id = {int(item["id"]): item for item in known}
    for item in refreshed:
        old_by_id[int(item["id"])] = item
    try:
        await db._set_setting(
            _CHANNEL_REGISTRY_KEY,
            json.dumps(
                list(old_by_id.values())[:_MAX_KNOWN_CHANNELS],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    except Exception as exc:
        print(f"[CAPTION_MAINTENANCE] channel registry refresh save failed: {exc}")

    channels.sort(key=lambda item: (str(item["title"]).lower(), item["id"]))
    return channels


def _channel_keyboard(channels, page=0):
    total_pages = max(
        1,
        (len(channels) + _CHANNELS_PER_PAGE - 1) // _CHANNELS_PER_PAGE,
    )
    page = max(0, min(int(page), total_pages - 1))
    start = page * _CHANNELS_PER_PAGE
    current = channels[start:start + _CHANNELS_PER_PAGE]

    rows = [
        [InlineKeyboardButton(
            _channel_label(item),
            callback_data=f"cap:channel:{int(item['id'])}",
        )]
        for offset, item in enumerate(current)
    ]

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "Previous", callback_data=f"cap:page:{page - 1}"
            )
        )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                "Next", callback_data=f"cap:page:{page + 1}"
            )
        )
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(
            "➕ Add / Check Channel",
            callback_data="cap:add",
        )
    ])
    rows.append([
        InlineKeyboardButton(
            "Refresh Channel List",
            callback_data="cap:refresh",
        )
    ])
    rows.append([InlineKeyboardButton("Cancel", callback_data="cap:cancel")])
    return InlineKeyboardMarkup(rows)


async def _resolve_target(chat_id, username=None):
    """Resolve and validate a maintenance target.

    Prefer a username when one is known. This hydrates the current Pyrogram
    bot peer for public channels that are already persisted in the registry,
    while numeric IDs remain the fallback for private channels already cached.
    """
    chat_id = int(chat_id)
    username = str(username or "").strip().lstrip("@")

    refs = []
    if username:
        refs.append(("@" + username, f"target lookup @{username}"))
    refs.append((chat_id, f"target lookup {chat_id}"))

    last_error = None
    chat = None
    for ref, label in refs:
        try:
            chat = await _flood(
                lambda target=ref: Bot.get_chat(target),
                label,
            )
            break
        except Exception as exc:
            last_error = exc

    if chat is None:
        if username:
            message = (
                "Telegram could not resolve the selected channel. "
                "Refresh the channel list and try again."
            )
        else:
            message = (
                "This private channel is not available in the current Telegram "
                "bot session. Press ➕ Add / Check Channel and forward one message "
                "from that channel to rebuild the channel peer, then select it again."
            )
        raise ValueError(message) from last_error

    if getattr(chat, "type", None) != enums.ChatType.CHANNEL:
        raise ValueError("Selected target is not a Telegram channel.")

    member = await _flood(
        lambda: Bot.get_chat_member(chat_id, "me"),
        f"target permission check {chat_id}",
    )
    status = str(getattr(member, "status", "")).lower()
    is_creator = "creator" in status or "owner" in status
    if not (is_creator or "administrator" in status):
        raise ValueError("The bot is no longer an administrator in this channel.")

    privileges = getattr(member, "privileges", None)
    if not is_creator and privileges is not None:
        if hasattr(privileges, "can_edit_messages") and not privileges.can_edit_messages:
            raise ValueError("The bot administrator cannot edit channel messages.")

    return chat


async def _latest_message_id(chat_id):
    async for message in Bot.get_chat_history(int(chat_id), limit=1):
        if message is not None and getattr(message, "id", None):
            return int(message.id)
    raise ValueError("Could not determine the latest message ID.")



def _render_set_caption(template, message):
    """Render Set Caption placeholders separately for each media message."""
    template = str(template or "")
    try:
        name, size = send_file._file_meta(message)
        size_text = send_file.human_size(size)
    except Exception:
        name, size_text = "Telegram Media", "Unknown"
    user = getattr(message, "from_user", None)
    values = {
        "file_name": str(name or "Telegram Media"),
        "file_size": str(size_text or "Unknown"),
        "caption": str(getattr(message, "caption", None) or ""),
        "username": str(getattr(user, "username", "") or "") if user else "",
        "user_id": str(getattr(user, "id", "") or "") if user else "",
        "first_name": str(getattr(user, "first_name", "") or "") if user else "",
    }
    is_html = bool(re.search(r"</?(?:b|strong|i|em|u|s|code|pre|a)(?:\\s|>)", template, re.I))
    for key, value in values.items():
        replacement = html.escape(value, quote=True) if is_html else value
        template = template.replace("{" + key + "}", replacement)
    if _caption_units(template) > 1024:
        raise ValueError("Rendered caption exceeds Telegram's 1024-character limit.")
    return template, ("html" if is_html else None)

def _make_job(session, dry_run=False):
    return {
        "status": "running",
        "run_mode": "normal",
        "dry_run": bool(dry_run),
        "operation": session["operation"],
        "chat_id": int(session["chat_id"]),
        "target_title": str(session.get("target_title") or ""),
        "target_username": str(session.get("target_username") or ""),
        "start_id": int(session["start_id"]),
        "end_id": int(session["end_id"]),
        "find": session.get("find", ""),
        "replace": session.get("replace", ""),
        "set_caption": session.get("set_caption", ""),
        "done_ids": [],
        "changed": 0,
        "skipped": 0,
        "failed_ids": [],
        "skipped_ids": [],
        "last_error": "",
        "fatal_error": "",
        "updated_at": int(time.time()),
    }


def _range_ids(job):
    start = int(job["start_id"])
    end = int(job["end_id"])
    step = 1 if end >= start else -1
    return list(range(start, end + step, step))


def _add_unique(values, value, limit=200000):
    value = int(value)
    if value in values:
        return
    if len(values) < limit:
        values.append(value)


def _remove_id(values, value):
    return [int(item) for item in values if int(item) != int(value)]


def _fatal_exception(exc):
    return exc.__class__.__name__ in {
        "ChatAdminRequired",
        "ChatWriteForbidden",
        "ChannelInvalid",
        "ChannelPrivate",
        "PeerIdInvalid",
        "UserNotParticipant",
    }


async def _edit_one(job, message):
    if getattr(message, "media", None) is None:
        return "skipped"

    old = getattr(message, "caption", None) or ""
    operation = job.get("operation")

    if operation == "replace":
        find_text = str(job.get("find", ""))
        if not old or not find_text or find_text not in old:
            return "skipped"

        new, occurrences = replace_caption_exact(
            old,
            find_text,
            str(job.get("replace", "")),
        )
        if occurrences <= 0 or new == old:
            return "skipped"

    elif operation == "set":
        new, parse_mode = _render_set_caption(str(job.get("set_caption", "")), message)
        if old == new:
            return "skipped"
    else:
        raise ValueError("Unknown caption maintenance operation.")

    if _caption_units(new) > 1024:
        raise ValueError("New caption exceeds Telegram's 1024-character limit.")

    if job.get("dry_run"):
        return "changed"

    kwargs = {
        "chat_id": int(job["chat_id"]),
        "message_id": int(message.id),
        "caption": new,
        "parse_mode": parse_mode if operation == "set" else None,
    }

    if operation == "replace":
        entities = getattr(message, "caption_entities", None)
        if entities is not None:
            kwargs["caption_entities"] = (
                adjust_caption_entities(
                    old, new, entities, job["find"], job["replace"]
                ) or []
            )
    else:
        kwargs["caption_entities"] = []

    try:
        await _flood(
            lambda: Bot.edit_message_caption(**kwargs),
            f"edit {job['chat_id']}/{message.id}",
        )
    except Exception as exc:
        if exc.__class__.__name__ == "MessageNotModified":
            return "skipped"
        raise

    return "changed"


async def _process_batch(job, batch_ids, runtime, status_message):
    try:
        fetched = await _flood(
            lambda: Bot.get_messages(
                chat_id=int(job["chat_id"]),
                message_ids=list(batch_ids),
            ),
            f"fetch {batch_ids[0]}-{batch_ids[-1]}",
        )
    except Exception as exc:
        for message_id in batch_ids:
            _add_unique(job["failed_ids"], message_id)
            _add_unique(job["done_ids"], message_id)
        job["last_error"] = f"Fetch {batch_ids[0]}-{batch_ids[-1]}: {exc}"
        await _set_job(job)
        return

    if isinstance(fetched, (list, tuple)):
        messages = {
            int(item.id): item
            for item in fetched
            if item is not None and getattr(item, "id", None)
        }
    elif fetched is not None and getattr(fetched, "id", None):
        messages = {int(fetched.id): fetched}
    else:
        messages = {}

    cursor = 0
    cursor_lock = asyncio.Lock()

    async def worker():
        nonlocal cursor

        while True:
            if runtime["stop"].is_set():
                return
            await runtime["pause"].wait()
            if runtime["stop"].is_set():
                return

            async with cursor_lock:
                if cursor >= len(batch_ids):
                    return
                message_id = int(batch_ids[cursor])
                cursor += 1

            completed = False
            try:
                message = messages.get(message_id)
                if message is None:
                    result = "skipped"
                else:
                    result = await _edit_one(job, message)

                completed = True
                if result == "changed":
                    job["changed"] = int(job.get("changed", 0)) + 1
                else:
                    job["skipped"] = int(job.get("skipped", 0)) + 1
                    _add_unique(job["skipped_ids"], message_id)

                if str(job.get("run_mode")) == "retry":
                    if result in {"changed", "skipped"}:
                        job["failed_ids"] = _remove_id(
                            job.get("failed_ids") or [],
                            message_id,
                        )

            except Exception as exc:
                _add_unique(job["failed_ids"], message_id)
                job["last_error"] = f"Message {message_id}: {exc}"
                if _fatal_exception(exc):
                    job["fatal_error"] = str(exc)
                    runtime["stop"].set()

            finally:
                if completed:
                    _add_unique(job["done_ids"], message_id)

                processed, _ = _progress(job)
                if processed and processed % CHECKPOINT_EVERY == 0:
                    await _set_job(job)

                if status_message and processed and processed % _STATUS_EVERY == 0:
                    try:
                        await _flood(
                            lambda: status_message.edit_text(
                                _status_text(job),
                                reply_markup=_keyboard(job, active=True),
                            ),
                            "progress update",
                        )
                    except Exception as exc:
                        print(
                            "[CAPTION_MAINTENANCE] status update failed: "
                            f"{exc}"
                        )

                if EDIT_DELAY:
                    await asyncio.sleep(EDIT_DELAY)

    await asyncio.gather(*(worker() for _ in range(WORKERS)))


async def _send_report(status_message, job, key, label):
    ids = [int(item) for item in job.get(key) or []]
    if not ids:
        return

    path = None
    try:
        fd, path = tempfile.mkstemp(
            prefix="caption-maintenance-",
            suffix=".txt",
        )
        os.close(fd)

        username = job.get("target_username") or None
        with open(path, "w", encoding="utf-8") as report:
            for message_id in ids:
                link = _link(job["chat_id"], message_id, username)
                report.write((link or str(message_id)) + "\n")

        await _flood(
            lambda: status_message.reply_document(
                path,
                caption=f"{label}: {len(ids)}",
            ),
            f"send {label}",
        )
    except Exception as exc:
        print(f"[CAPTION_MAINTENANCE] {label} report failed: {exc}")
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


async def _run_job(job, status_message):
    global _RUNTIME

    runtime = {
        "pause": asyncio.Event(),
        "stop": asyncio.Event(),
        "job": job,
    }
    runtime["pause"].set()
    _RUNTIME = runtime

    try:
        all_ids = _range_ids(job)
        done = {int(item) for item in job.get("done_ids") or []}

        if str(job.get("run_mode")) == "retry":
            source_ids = [
                int(item) for item in job.get("retry_ids") or []
            ]
        else:
            source_ids = all_ids

        while not runtime["stop"].is_set():
            await runtime["pause"].wait()

            if runtime["stop"].is_set():
                break

            done = {int(item) for item in job.get("done_ids") or []}
            pending = [item for item in source_ids if item not in done]
            if not pending:
                break

            await _process_batch(
                job,
                pending[:FETCH_BATCH],
                runtime,
                status_message,
            )

        if runtime["stop"].is_set():
            job["status"] = (
                "failed" if job.get("fatal_error") else "stopped"
            )
        else:
            job["status"] = "completed"

    except asyncio.CancelledError:
        job["status"] = "stopped"
        raise
    except Exception as exc:
        job["status"] = "failed"
        job["fatal_error"] = str(exc)
        print(f"[CAPTION_MAINTENANCE] job failed: {exc}")

    finally:
        await _set_job(job)

        try:
            await _flood(
                lambda: status_message.edit_text(
                    _status_text(job),
                    reply_markup=_keyboard(job, active=False),
                ),
                "final status",
            )
        except Exception as exc:
            print(
                "[CAPTION_MAINTENANCE] final status update failed: "
                f"{exc}"
            )

        await _send_report(
            status_message, job, "skipped_ids", "SKIPPED MSG LINKS"
        )
        await _send_report(
            status_message, job, "failed_ids", "FAILED MSG LINKS"
        )
        _RUNTIME = None


def _start_task(job, status_message):
    global _JOB_TASK

    if _JOB_TASK is not None and not _JOB_TASK.done():
        raise ValueError("Another caption maintenance job is already running.")

    task = asyncio.create_task(_run_job(job, status_message))
    _JOB_TASK = task

    def done_callback(done_task):
        global _JOB_TASK
        if _JOB_TASK is done_task:
            _JOB_TASK = None

    task.add_done_callback(done_callback)


def _session_start(user_id, operation):
    _SESSIONS[int(user_id)] = {
        "step": "channels",
        "operation": operation,
        "channels": [],
    }


def _review(session, storage_warning=False):
    operation = (
        "Caption Replace"
        if session["operation"] == "replace"
        else "Set Caption"
    )
    lines = [
        f"**{operation} — Confirm**",
        "",
        f"Target: {session.get('target_title') or session['chat_id']}",
        f"Channel ID: {session['chat_id']}",
        f"Range: {session['start_id']} -> {session['end_id']}",
        f"Messages: {abs(session['end_id'] - session['start_id']) + 1}",
        "",
    ]

    if session["operation"] == "replace":
        lines.extend([
            f"FIND: {session['find']}",
            f"REPLACE: {session['replace']}",
            "",
            "Only captions containing the exact FIND text will change.",
            "Everything else in those captions remains untouched.",
        ])
    else:
        lines.extend([
            f"SET CAPTION TEMPLATE: {session['set_caption']}",
            "",
            "Placeholders are rendered separately for every media message:",
            "{file_name} = original file name",
            "{file_size} = human-readable file size",
            "{caption} = original caption",
            "{username}, {user_id}, {first_name} = original sender fields",
            "HTML tags such as <code>, <i>, <b> are supported automatically.",
            "Text-only messages are skipped because they have no caption.",
        ])

    if storage_warning:
        lines.extend([
            "",
            "WARNING: This is the configured HJ storage channel.",
            "Confirm only if you intentionally selected the storage channel.",
        ])

    return "\n".join(lines)


async def _normalize_saved_job(job):
    if not job:
        return None

    state = str(job.get("status", "")).lower()
    if state == "running" and (_JOB_TASK is None or _JOB_TASK.done()):
        job["status"] = "stopped"
        job["last_error"] = (
            "Previous worker session ended before completion. Resume to continue."
        )
        await _set_job(job)
    elif state == "stopping":
        job["status"] = "stopped"
        await _set_job(job)
    return job


async def _show_saved(message):
    job = await _normalize_saved_job(await _get_job())
    if not job:
        await message.reply_text(
            "No saved caption maintenance job. Use /caption_replace or /set_caption."
        )
        return

    await message.reply_text(
        _status_text(job),
        reply_markup=_keyboard(
            job,
            active=_JOB_TASK is not None and not _JOB_TASK.done(),
        ),
    )


async def _start_new(message, operation):
    job = await _normalize_saved_job(await _get_job())
    state = str(job.get("status", "")).lower() if job else ""
    if job and state in {"running", "paused", "stopping"}:
        await _show_saved(message)
        return

    # An explicit /caption_replace or /set_caption starts a fresh job after
    # an older job has stopped/failed/completed.
    if job and state in {"stopped", "failed", "completed"}:
        await db._set_setting("caption_maintenance_job", "")

    _session_start(message.from_user.id, operation)
    status = await message.reply_text(
        "Loading known Telegram channels where this bot can edit messages..."
    )

    try:
        channels = await _discover_admin_channels()
    except Exception as exc:
        await status.edit_text(
            f"Channel list refresh failed: {exc}\n\n"
            "Use ➕ Add / Check Channel and send @username or forward one channel message."
        )
        return

    session = _SESSIONS[int(message.from_user.id)]
    session["channels"] = channels

    if not channels:
        text = (
            f"{operation.title().replace('_', ' ')} — Select Target Channel\n\n"
            "No previously-known editable channels are currently resolvable.\n\n"
            "Use ➕ Add / Check Channel, then send the channel @username, "
            "numeric ID, or forward one message from the target channel."
        )
    else:
        text = (
            f"{operation.title().replace('_', ' ')} — Select Target Channel\n\n"
            f"Found {len(channels)} currently available editable channel(s).\n"
            "Private channels that are not yet in the current Pyrogram peer cache "
            "can be re-added by forwarding one message from that channel.\n"
            "HJ storage is marked separately."
        )

    await status.edit_text(
        text,
        reply_markup=_channel_keyboard(channels, 0),
    )


@Bot.on_message(filters.channel, group=-10)
async def _observe_channel_message(_, message):
    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "type", None) != enums.ChatType.CHANNEL:
        return
    chat_id = int(chat.id)
    seen = _RUNTIME_CHANNEL_SEEN
    if chat_id in seen:
        return
    seen.add(chat_id)
    try:
        await _remember_channel(chat)
    except Exception as exc:
        print(
            f"[CAPTION_MAINTENANCE] channel observation failed for "
            f"{chat_id}: {exc}"
        )


@Bot.on_chat_member_updated(group=-10)
async def _observe_channel_membership(_, update):
    chat = getattr(update, "chat", None)
    if chat is None or getattr(chat, "type", None) != enums.ChatType.CHANNEL:
        return

    old_member = getattr(update, "old_chat_member", None)
    new_member = getattr(update, "new_chat_member", None)
    old_user = getattr(old_member, "user", None)
    new_user = getattr(new_member, "user", None)
    try:
        me = await Bot.get_me()
        bot_id = int(me.id)
    except Exception:
        return

    if not (
        (old_user is not None and int(old_user.id) == bot_id)
        or (new_user is not None and int(new_user.id) == bot_id)
    ):
        return

    status = str(getattr(new_member, "status", "")).lower()
    is_admin = "administrator" in status or "creator" in status or "owner" in status
    if is_admin:
        try:
            await _remember_channel(chat)
        except Exception as exc:
            print(
                f"[CAPTION_MAINTENANCE] membership registration failed for "
                f"{chat.id}: {exc}"
            )


@Bot.on_message(filters.private & filters.command("caption_replace"), group=-3)
async def caption_replace_command(_, message):
    if not _owner(message.from_user.id):
        await message.reply_text("Owner/Admin Only")
        raise StopPropagation
    await _start_new(message, "replace")
    raise StopPropagation


@Bot.on_message(filters.private & filters.command("set_caption"), group=-3)
async def set_caption_command(_, message):
    if not _owner(message.from_user.id):
        await message.reply_text("Owner/Admin Only")
        raise StopPropagation
    await _start_new(message, "set")
    raise StopPropagation




@Bot.on_message(
    filters.private & filters.forwarded,
    group=-4,
)
async def caption_maintenance_forwarded(_, message):
    user_id = int(message.from_user.id)
    session = _SESSIONS.get(user_id)
    if not session or session.get("step") != "add_channel":
        return

    if not _owner(user_id):
        _SESSIONS.pop(user_id, None)
        await message.reply_text("Owner/Admin Only")
        raise StopPropagation

    chat = _forward_source(message)
    if chat is None or getattr(chat, "type", None) != enums.ChatType.CHANNEL:
        await message.reply_text(
            "❌ Please forward one message directly from the target Telegram channel."
        )
        raise StopPropagation

    try:
        await _resolve_target(int(chat.id))
        await _remember_channel(chat)
        channels = await _discover_admin_channels()
        session["channels"] = channels
        session["step"] = "channels"

        await message.reply_text(
            f"✅ Channel verified and added: {getattr(chat, 'title', '') or chat.id}\n"
            "Select the target channel below.",
            reply_markup=_channel_keyboard(channels, 0),
        )
    except Exception as exc:
        await message.reply_text(
            f"❌ Could not verify that channel: {exc}\n"
            "Make sure the bot is an administrator and can edit channel messages."
        )
    raise StopPropagation
@Bot.on_message(
    filters.private & filters.text & ~filters.command(bot_legacy.FILESTORE_COMMANDS),
    group=-3,
)
async def caption_maintenance_input(_, message):
    user_id = int(message.from_user.id)
    session = _SESSIONS.get(user_id)
    if not session:
        return

    if not _owner(user_id):
        _SESSIONS.pop(user_id, None)
        await message.reply_text("Owner/Admin Only")
        raise StopPropagation

    value = message.text
    try:
        if value.strip().lower() == "/cancel":
            _SESSIONS.pop(user_id, None)
            await message.reply_text("Caption maintenance setup cancelled.")
            raise StopPropagation

        step = session.get("step")

        if step == "add_channel":
            ref = _channel_input_ref(value)
            try:
                chat = await _flood(
                    lambda target=ref: Bot.get_chat(target),
                    f"add channel {ref}",
                )
            except Exception as exc:
                raise ValueError(
                    f"Telegram could not resolve that channel: {exc}\n"
                    "For a private channel, forward one message from the channel and try again."
                )

            if getattr(chat, "type", None) != enums.ChatType.CHANNEL:
                raise ValueError("That target is not a Telegram channel.")

            await _resolve_target(int(chat.id))
            await _remember_channel(chat)

            channels = await _discover_admin_channels()
            session["channels"] = channels
            session["step"] = "channels"

            await message.reply_text(
                f"✅ Channel added: {getattr(chat, 'title', '') or chat.id}\n"
                "Select it from the channel list below.",
                reply_markup=_channel_keyboard(channels, 0),
            )
            raise StopPropagation

        if step == "start":
            if not value.strip().isdigit() or int(value) <= 0:
                raise ValueError("START message ID must be a positive number.")
            session.update(step="end", start_id=int(value))
            await message.reply_text("Send the STOP / END message ID.")

        elif step == "end":
            if not value.strip().isdigit() or int(value) <= 0:
                raise ValueError("STOP / END message ID must be a positive number.")
            session["end_id"] = int(value)

            if abs(session["end_id"] - session["start_id"]) + 1 > _MAX_RANGE:
                raise ValueError(
                    f"One maintenance range cannot exceed {_MAX_RANGE} messages."
                )

            session["step"] = (
                "find"
                if session["operation"] == "replace"
                else "caption"
            )
            if session["operation"] == "replace":
                await message.reply_text(
                    "Send the exact FIND text.\n"
                    "Matching is literal, case-sensitive, and keeps spaces/newlines."
                )
            else:
                await message.reply_text(
                    "Send the caption template to set.\n\n"
                    "Supported: {file_name}, {file_size}, {caption}, {username}, {user_id}, {first_name}\n"
                    "Example:\n"
                    "<code>• 🎥 File Name : {file_name}</code><i> </i>\n"
                    "<code>• 💾 File size : {file_size}</code><i> </i>\n"
                    "• ❤️ @hjgroups_1\n\n"
                    "Each message gets its own file name and size automatically. HTML tags such as <code>/<i>/<b> are supported."
                )

        elif step == "range":
            raw = value.strip()
            if raw.lower() == "all":
                session["start_id"] = 1
                session["end_id"] = await _latest_message_id(session["chat_id"])
            else:
                parts = raw.split()
                if len(parts) != 2 or not all(
                    part.isdigit() and int(part) > 0 for part in parts
                ):
                    raise ValueError(
                        "Send ALL or two positive message IDs such as 1 500."
                    )
                session["start_id"] = int(parts[0])
                session["end_id"] = int(parts[1])

            if abs(session["end_id"] - session["start_id"]) + 1 > _MAX_RANGE:
                raise ValueError(
                    f"One maintenance range cannot exceed {_MAX_RANGE} messages."
                )

            session["step"] = "caption"
            await message.reply_text(
                f"Range selected: {session['start_id']} -> {session['end_id']}\n\n"
                "Send the caption template to set. Use {file_name} and {file_size} for automatic per-file values."
            )

        elif step == "find":
            if not value or _caption_units(value) > 1024:
                raise ValueError("FIND text must be 1–1024 Telegram characters.")
            session.update(step="replace", find=value)
            await message.reply_text("Send the REPLACE text.")

        elif step == "replace":
            if not value or _caption_units(value) > 1024:
                raise ValueError("REPLACE text must be 1–1024 Telegram characters.")

            session.update(step="review", replace=value)
            storage_selected = await _storage_selected(session["chat_id"])
            await message.reply_text(
                _review(session, storage_warning=storage_selected),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Start", callback_data="cap:start"),
                        InlineKeyboardButton("Dry Run", callback_data="cap:dry"),
                    ],
                    [InlineKeyboardButton("Cancel", callback_data="cap:cancel")],
                ]),
            )

        elif step == "caption":
            if not value or _caption_units(value) > 1024:
                raise ValueError("Caption must be 1–1024 Telegram characters.")

            session.update(step="review", set_caption=value)
            storage_selected = await _storage_selected(session["chat_id"])
            await message.reply_text(
                _review(session, storage_warning=storage_selected),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Start", callback_data="cap:start"),
                        InlineKeyboardButton("Dry Run", callback_data="cap:dry"),
                    ],
                    [InlineKeyboardButton("Cancel", callback_data="cap:cancel")],
                ]),
            )

        else:
            await message.reply_text(
                "Use the buttons above or /cancel."
            )

    except StopPropagation:
        raise
    except Exception as exc:
        await message.reply_text(f"Error: {exc}")

    raise StopPropagation


async def _storage_selected(chat_id):
    try:
        return int(chat_id) in {
            int(item) for item in await db.get_storage_channels()
        }
    except Exception:
        return False


async def _create_job_from_session(user_id, dry_run=False):
    session = _SESSIONS.get(int(user_id))
    if not session or session.get("step") != "review":
        raise ValueError("The setup session expired. Run the command again.")

    await _resolve_target(
        int(session["chat_id"]),
        session.get("target_username"),
    )

    if await _storage_selected(session["chat_id"]):
        raise RuntimeError("STORAGE_CONFIRM_REQUIRED")

    job = _make_job(session, dry_run=dry_run)
    _SESSIONS.pop(int(user_id), None)
    await _set_job(job)
    return job


@Bot.on_callback_query(filters.regex(r"^cap:"), group=-3)
async def caption_maintenance_callback(_, query):
    global _RUNTIME

    user_id = int(query.from_user.id)
    if not _owner(user_id):
        await query.answer("Owner/Admin Only", show_alert=True)
        raise StopPropagation

    try:
        await query.answer()
    except Exception:
        pass

    parts = str(query.data).split(":")
    action = parts[1] if len(parts) > 1 else ""

    try:
        if action == "cancel":
            _SESSIONS.pop(user_id, None)
            await query.message.edit_text("Caption maintenance setup cancelled.")
            raise StopPropagation

        if action == "add":
            session = _SESSIONS.get(user_id)
            if not session or session.get("step") != "channels":
                raise ValueError("Channel selector expired. Run the command again.")

            session["step"] = "add_channel"
            await query.message.edit_text(
                "➕ Add / Check Channel\n\n"
                "Send the target channel @username, t.me/username, or numeric channel ID.\n"
                "For private channels, the safest method is to forward one message "
                "from that channel to this chat.\n\n"
                "The bot must be an Administrator and have Edit Messages permission."
            )
            raise StopPropagation

        if action == "refresh":
            session = _SESSIONS.get(user_id)
            if not session or session.get("step") != "channels":
                raise ValueError("Channel selector expired. Run the command again.")

            channels = await _discover_admin_channels()
            session["channels"] = channels
            await query.message.edit_text(
                f"{session['operation'].title().replace('_', ' ')} — Select Target Channel\n\n"
                f"Found {len(channels)} editable channel(s).",
                reply_markup=_channel_keyboard(channels, 0),
            )
            raise StopPropagation

        if action == "page":
            session = _SESSIONS.get(user_id)
            if not session or session.get("step") != "channels":
                raise ValueError("Channel selector expired. Run the command again.")

            markup = _channel_keyboard(
                session.get("channels") or [],
                int(parts[2]),
            )
            await query.message.edit_reply_markup(markup)
            raise StopPropagation

        if action == "channel":
            session = _SESSIONS.get(user_id)
            if not session or session.get("step") != "channels":
                raise ValueError("Channel selector expired. Run the command again.")

            selector = int(parts[2])
            channels = session.get("channels") or []

            # Current buttons carry the stable Telegram channel ID. Keep
            # backward compatibility with older keyboards that carried an
            # array index.
            selected = next(
                (
                    item for item in channels
                    if int(item.get("id", 0)) == selector
                ),
                None,
            )
            if selected is None and 0 <= selector < len(channels):
                selected = channels[selector]
            if selected is None:
                raise ValueError(
                    "Channel selection expired. Refresh the channel list and try again."
                )

            try:
                chat = await _resolve_target(
                    int(selected["id"]),
                    selected.get("username"),
                )
            except Exception as exc:
                print(
                    "[CAPTION_MAINTENANCE] channel selection failed: "
                    f"id={selected.get('id')} title={selected.get('title')} "
                    f"username={selected.get('username') or '-'} error={exc}"
                )
                raise

            session.update(
                step="range",
                chat_id=int(selected["id"]),
                target_title=getattr(chat, "title", "") or selected["title"],
                target_username=getattr(chat, "username", "") or selected["username"],
            )

            if session["operation"] == "replace":
                session["step"] = "start"
                prompt = (
                    "Selected channel: "
                    f"{session['target_title']} ({session['chat_id']})\n\n"
                    "Send the START message ID."
                )
            else:
                prompt = (
                    "Selected channel: "
                    f"{session['target_title']} ({session['chat_id']})\n\n"
                    "Send ALL for the entire channel, or two IDs such as 1 500."
                )

            await query.message.edit_text(prompt)
            raise StopPropagation

        if action == "new":
            if _JOB_TASK is not None and not _JOB_TASK.done():
                raise ValueError("Stop the current job first.")
            _SESSIONS.pop(user_id, None)
            await query.message.reply_text(
                "Use /caption_replace or /set_caption to start a new maintenance job."
            )
            raise StopPropagation

        if action in {"start", "dry"}:
            session = _SESSIONS.get(user_id)
            if not session or session.get("step") != "review":
                raise ValueError("The setup session expired. Run the command again.")

            if _JOB_TASK is not None and not _JOB_TASK.done():
                raise ValueError("Another caption maintenance job is already running.")

            try:
                job = await _create_job_from_session(
                    user_id,
                    dry_run=(action == "dry"),
                )
            except RuntimeError as exc:
                if str(exc) != "STORAGE_CONFIRM_REQUIRED":
                    raise

                await query.message.edit_text(
                    _review(session, storage_warning=True),
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "Confirm Storage Edit",
                                callback_data=f"cap:confirm:{action}",
                            ),
                            InlineKeyboardButton(
                                "Cancel",
                                callback_data="cap:cancel",
                            ),
                        ]
                    ]),
                )
                raise StopPropagation

            await query.message.edit_text(
                _status_text(job),
                reply_markup=_keyboard(job, active=True),
            )
            _start_task(job, query.message)
            raise StopPropagation

        if action == "confirm":
            session = _SESSIONS.get(user_id)
            if not session or session.get("step") != "review":
                raise ValueError("The setup session expired. Run the command again.")

            await _resolve_target(int(session["chat_id"]), session.get("target_username"))
            job = _make_job(
                session,
                dry_run=(len(parts) > 2 and parts[2] == "dry"),
            )
            _SESSIONS.pop(user_id, None)
            await _set_job(job)

            await query.message.edit_text(
                _status_text(job),
                reply_markup=_keyboard(job, active=True),
            )
            _start_task(job, query.message)
            raise StopPropagation

        job = _RUNTIME["job"] if _RUNTIME is not None else await _get_job()
        if not job:
            raise ValueError("No saved caption maintenance job found.")

        state = str(job.get("status", "")).lower()

        if action == "status":
            await query.message.edit_text(
                _status_text(job),
                reply_markup=_keyboard(
                    job,
                    active=_JOB_TASK is not None and not _JOB_TASK.done(),
                ),
            )
            raise StopPropagation

        if action == "pause":
            if _RUNTIME is None or _JOB_TASK is None or _JOB_TASK.done():
                raise ValueError("Worker is not active. Use Resume.")

            _RUNTIME["pause"].clear()
            job["status"] = "paused"
            await _set_job(job)
            await query.message.edit_text(
                _status_text(job),
                reply_markup=_keyboard(job, active=False),
            )
            raise StopPropagation

        if action == "resume":
            if _JOB_TASK is not None and not _JOB_TASK.done():
                if _RUNTIME is not None:
                    _RUNTIME["pause"].set()
                    job["status"] = "running"
                    await _set_job(job)
                    await query.message.edit_text(
                        _status_text(job),
                        reply_markup=_keyboard(job, active=True),
                    )
                raise StopPropagation

            if state not in {"running", "paused", "stopped", "failed"}:
                raise ValueError(
                    "This job is completed. Use New Job or Retry Failed IDs."
                )

            await _resolve_target(int(job["chat_id"]))
            job["status"] = "running"
            job["fatal_error"] = ""
            await _set_job(job)
            await query.message.edit_text(
                _status_text(job),
                reply_markup=_keyboard(job, active=True),
            )
            _start_task(job, query.message)
            raise StopPropagation

        if action == "stop":
            if _RUNTIME is not None and _JOB_TASK is not None and not _JOB_TASK.done():
                _RUNTIME["stop"].set()
                job["status"] = "stopping"
                await _set_job(job)
                await query.message.edit_text(
                    _status_text(job),
                    reply_markup=_keyboard(job, active=False),
                )
            else:
                job["status"] = "stopped"
                await _set_job(job)
                await query.message.edit_text(
                    _status_text(job),
                    reply_markup=_keyboard(job, active=False),
                )
            raise StopPropagation

        if action == "retry":
            if _JOB_TASK is not None and not _JOB_TASK.done():
                raise ValueError("Another caption maintenance job is already running.")

            failed = [int(item) for item in job.get("failed_ids") or []]
            if not failed:
                raise ValueError("There are no failed message IDs to retry.")

            await _resolve_target(int(job["chat_id"]))
            retry_job = copy.deepcopy(job)
            retry_job.update({
                "status": "running",
                "run_mode": "retry",
                "retry_ids": failed,
                "retry_total": len(failed),
                "done_ids": [],
                "skipped_ids": [],
                "changed": 0,
                "skipped": 0,
                "last_error": "",
                "fatal_error": "",
                "failed_ids": [],
            })
            await _set_job(retry_job)

            await query.message.edit_text(
                _status_text(retry_job),
                reply_markup=_keyboard(retry_job, active=True),
            )
            _start_task(retry_job, query.message)
            raise StopPropagation

        raise ValueError("Unknown maintenance action.")

    except StopPropagation:
        raise
    except Exception as exc:
        # The callback query is already answered at handler entry. A second
        # query.answer() can fail with QUERY_ID_INVALID and hide the real error.
        try:
            await query.message.reply_text(f"❌ {str(exc)[:3500]}")
        except Exception as report_error:
            print(
                "[CAPTION_MAINTENANCE] callback error reporting failed: "
                f"{report_error}"
            )

    raise StopPropagation