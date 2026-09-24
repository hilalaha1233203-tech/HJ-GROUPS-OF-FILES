"""Owner-only bulk exact caption replacement for explicitly allow-listed Telegram channels.

This module is isolated from the FileStore save/delivery flows. It uses the existing
Pyrogram client and existing Supabase bot_settings key/value store for checkpoints.
"""
import asyncio
import copy
import os
import tempfile
import time

from pyrogram import enums, filters, StopPropagation
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import bot_legacy
from configs import Config
from handlers.database import db

Bot = bot_legacy.Bot
_SESSIONS = {}
_RUNTIME = None
_JOB_TASK = None
_PERSIST_LOCK = asyncio.Lock()
_STATUS_EVERY = 25


def _owner(user_id):
    return bool(Config.BOT_OWNER) and int(user_id) == int(Config.BOT_OWNER)


def _env_int(name, default, low=None, high=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _allowed_channels():
    result = set()
    for raw in os.environ.get("CAPTION_REPLACE_ALLOWED_CHANNELS", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            result.add(int(raw))
        except ValueError:
            continue
    return result


WORKERS = _env_int("CAPTION_REPLACE_WORKERS", 2, 2, 3)
FETCH_BATCH = _env_int("CAPTION_REPLACE_FETCH_BATCH", 100, 1, 200)
CHECKPOINT_EVERY = _env_int("CAPTION_REPLACE_CHECKPOINT_EVERY", 10, 1, 1000)
EDIT_DELAY = max(0.0, float(os.environ.get("CAPTION_REPLACE_EDIT_DELAY", "0.15")))


def _link(chat_id, message_id, username=None):
    if username:
        return f"https://t.me/{username.lstrip('@')}/{int(message_id)}"
    value = str(abs(int(chat_id)))
    if value.startswith("100"):
        return f"https://t.me/c/{value[3:]}/{int(message_id)}"
    return None


def _u16(text):
    return len(str(text).encode("utf-16-le")) // 2


def _u16_index(text, units):
    target = max(0, int(units))
    used = 0
    for index, char in enumerate(str(text)):
        if used >= target:
            return index
        used += 2 if ord(char) > 0xFFFF else 1
    return len(str(text))


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
    """Keep unaffected/containing entities using Telegram's UTF-16 offsets.

    Entities that cross only part of the replaced phrase are dropped rather than
    risking malformed formatting. The caption text itself is always preserved.
    """
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
            overlaps = [s for s in spans if max(start, s[0]) < min(end, s[1])]
            if any(not (s[0] >= start and s[1] <= end) for s in overlaps):
                continue
            new_start = _map_index(start, spans)
            new_end = _map_index(end, spans)
            if new_end < new_start:
                continue
            adjusted = copy.deepcopy(entity)
            adjusted.offset = _u16(new_caption[:new_start])
            adjusted.length = _u16(new_caption[new_start:new_end])
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
            print(f"[CAPTION_REPLACE] FloodWait {label}: waiting {wait_seconds}s")
            await asyncio.sleep(wait_seconds)


async def _get_job():
    raw = await db._get_setting("caption_replace_job", "")
    if not raw:
        return None
    try:
        value = __import__("json").loads(raw)
    except Exception as exc:
        print(f"[CAPTION_REPLACE] Invalid saved job: {exc}")
        return None
    return value if isinstance(value, dict) else None


async def _set_job(job):
    import json
    job["updated_at"] = int(time.time())
    try:
        async with _PERSIST_LOCK:
            await db._set_setting(
                "caption_replace_job",
                json.dumps(job, ensure_ascii=False, separators=(",", ":")),
            )
    except Exception as exc:
        print(f"[CAPTION_REPLACE] checkpoint failed: {exc}")


def _total(job):
    return abs(int(job["end_id"]) - int(job["start_id"])) + 1


def _status_text(job):
    mode = str(job.get("mode", "edit")).upper()
    status = str(job.get("status", "unknown")).upper()
    retry = mode == "RETRY"
    processed = int(job.get("retry_index", 0)) if retry else int(job.get("next_index", 0))
    total = int(job.get("retry_total", 0)) if retry else _total(job)
    lines = [
        "**Caption Replace**",
        f"Status: {status} | Mode: {mode}",
        f"Target: {job.get('chat_id')}",
        f"Range: {job.get('start_id')} → {job.get('end_id')}",
        f"Progress: {processed} / {total} processed",
    ]
    if job.get("dry_run"):
        lines.append(f"Would change: {job.get('changed', 0)}")
    else:
        lines.append(f"Changed: {job.get('changed', 0)}")
    lines.append(f"Skipped: {job.get('skipped', 0)}")
    lines.append(f"Failed: {len(job.get('failed_ids') or [])}")
    if job.get("last_error"):
        lines.append(f"Last error: {str(job['last_error'])[:700]}")
    if job.get("fatal_error"):
        lines.append(f"Job error: {str(job['fatal_error'])[:700]}")
    return "\n".join(lines)


def _keyboard(job, active=False):
    state = str(job.get("status", "")).lower()
    rows = []
    if state == "running":
        rows.append([InlineKeyboardButton(
            "⏸ Pause" if active else "▶️ Resume",
            callback_data="cr:pause" if active else "cr:resume",
        )])
        rows.append([InlineKeyboardButton("⛔ Stop", callback_data="cr:stop")])
    elif state == "paused":
        rows.append([InlineKeyboardButton("▶️ Resume", callback_data="cr:resume")])
        rows.append([InlineKeyboardButton("⛔ Stop", callback_data="cr:stop")])
    elif state in {"stopped", "failed"}:
        if str(job.get("mode", "")) == "retry":
            can_resume = int(job.get("retry_index", 0)) < int(job.get("retry_total", 0))
        else:
            can_resume = int(job.get("next_index", 0)) < _total(job)
        if can_resume:
            rows.append([InlineKeyboardButton("▶️ Resume", callback_data="cr:resume")])
    if state in {"stopped", "failed", "completed"} and job.get("failed_ids"):
        rows.append([InlineKeyboardButton("🔁 Retry Failed IDs", callback_data="cr:retry")])
    if state in {"stopped", "failed", "completed"}:
        rows.append([InlineKeyboardButton("🆕 New Job", callback_data="cr:new")])
    rows.append([InlineKeyboardButton("📊 Refresh Status", callback_data="cr:status")])
    return InlineKeyboardMarkup(rows)


def _review(session):
    find_text = session["find"].replace("\n", "\\n")
    replace_text = session["replace"].replace("\n", "\\n")
    return (
        "**Caption Replace — Confirm**\n\n"
        f"Target channel: {session['chat_id']}\n"
        f"Channel: {session.get('target_title') or 'Unknown'}\n"
        f"Range: {session['start_id']} → {session['end_id']}\n"
        f"Messages: {abs(session['end_id'] - session['start_id']) + 1}\n\n"
        f"FIND: {find_text}\n"
        f"REPLACE: {replace_text}\n\n"
        "Only media captions containing the exact FIND text are edited.\n"
        "Other caption text, media and message IDs are untouched.\n"
        "Formatting is preserved where Telegram entities can be safely shifted.\n\n"
        "Admin-only maintenance tool."
    )


async def _resolve_target(chat_id):
    if not _allowed_channels():
        raise ValueError(
            "Caption Replace is disabled. Add the separate target channel ID to "
            "CAPTION_REPLACE_ALLOWED_CHANNELS in Voroa."
        )
    if int(chat_id) not in _allowed_channels():
        raise ValueError("That channel ID is not in CAPTION_REPLACE_ALLOWED_CHANNELS.")
    chat = await _flood(lambda: Bot.get_chat(int(chat_id)), "target lookup")
    if getattr(chat, "type", None) != enums.ChatType.CHANNEL:
        raise ValueError("Target must be a Telegram channel, not a group/supergroup.")
    member = await _flood(lambda: Bot.get_chat_member(int(chat_id), "me"), "permission check")
    status = str(getattr(member, "status", "")).lower()
    if not any(word in status for word in ("administrator", "owner", "creator")):
        raise ValueError("The bot is not an administrator of the target channel.")
    privileges = getattr(member, "privileges", None)
    if privileges is not None and hasattr(privileges, "can_edit_messages") and not privileges.can_edit_messages:
        raise ValueError("The bot administrator has no permission to edit channel messages.")
    return chat


def _make_job(session, dry_run=False):
    return {
        "status": "running",
        "mode": "edit",
        "dry_run": bool(dry_run),
        "chat_id": int(session["chat_id"]),
        "target_title": str(session.get("target_title") or ""),
        "target_username": str(getattr(session.get("chat"), "username", "") or ""),
        "start_id": int(session["start_id"]),
        "end_id": int(session["end_id"]),
        "find": session["find"],
        "replace": session["replace"],
        "next_index": 0,
        "processed": 0,
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


async def _edit_one(job, message):
    old = getattr(message, "caption", None)
    if not old or job["find"] not in old:
        return "skipped"
    new, occurrences = replace_caption_exact(old, job["find"], job["replace"])
    if occurrences <= 0 or new == old:
        return "skipped"
    if len(new) > 1024:
        raise ValueError("New caption exceeds Telegram's 1024-character limit.")
    if job.get("dry_run"):
        return "changed"

    entities = getattr(message, "caption_entities", None)
    kwargs = {
        "chat_id": int(job["chat_id"]),
        "message_id": int(message.id),
        "caption": new,
        "parse_mode": None,
    }
    if entities is not None:
        kwargs["caption_entities"] = adjust_caption_entities(
            old, new, entities, job["find"], job["replace"]
        ) or []
    try:
        await _flood(lambda: Bot.edit_message_caption(**kwargs), f"edit {message.id}")
    except Exception as exc:
        if exc.__class__.__name__ == "MessageNotModified":
            return "skipped"
        raise
    return "changed"


def _add_failed(job, message_id):
    failed = [int(x) for x in job.get("failed_ids") or []]
    failed.append(int(message_id))
    job["failed_ids"] = list(dict.fromkeys(failed))


async def _process_batch(job, batch_ids, runtime, status_message, retry=False):
    try:
        fetched = await _flood(
            lambda: Bot.get_messages(chat_id=int(job["chat_id"]), message_ids=batch_ids),
            f"fetch {batch_ids[0]}-{batch_ids[-1]}",
        )
    except Exception as exc:
        for message_id in batch_ids:
            _add_failed(job, message_id)
            job["processed"] = int(job.get("processed", 0)) + 1
            if retry:
                job["retry_index"] = int(job.get("retry_index", 0)) + 1
            else:
                job["next_index"] = int(job.get("next_index", 0)) + 1
        job["last_error"] = f"Fetch {batch_ids[0]}-{batch_ids[-1]}: {exc}"
        await _set_job(job)
        return

    messages = {}
    if isinstance(fetched, (list, tuple)):
        messages = {int(item.id): item for item in fetched if item is not None}
    elif fetched is not None:
        messages[int(fetched.id)] = fetched

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

            try:
                message = messages.get(message_id)
                if message is None or getattr(message, "media", None) is None:
                    result = "skipped"
                else:
                    result = await _edit_one(job, message)

                if result == "changed":
                    job["changed"] = int(job.get("changed", 0)) + 1
                else:
                    job["skipped"] = int(job.get("skipped", 0)) + 1
                    skipped_ids = job.setdefault("skipped_ids", [])
                    if len(skipped_ids) < 20000:
                        skipped_ids.append(message_id)

                if retry and result in {"changed", "skipped"}:
                    job["failed_ids"] = [
                        int(x) for x in job.get("failed_ids", [])
                        if int(x) != message_id
                    ]
            except Exception as exc:
                _add_failed(job, message_id)
                job["last_error"] = f"Message {message_id}: {exc}"
                if exc.__class__.__name__ in {
                    "ChatAdminRequired", "ChannelPrivate",
                    "PeerIdInvalid", "UserNotParticipant",
                }:
                    job["fatal_error"] = str(exc)
                    runtime["stop"].set()
            finally:
                job["processed"] = int(job.get("processed", 0)) + 1
                if retry:
                    job["retry_index"] = int(job.get("retry_index", 0)) + 1
                else:
                    job["next_index"] = int(job.get("next_index", 0)) + 1

                if job["processed"] % CHECKPOINT_EVERY == 0:
                    await _set_job(job)
                if status_message and job["processed"] % _STATUS_EVERY == 0:
                    try:
                        await _flood(
                            lambda: status_message.edit_text(
                                _status_text(job),
                                reply_markup=_keyboard(job, active=True),
                            ),
                            "progress update",
                        )
                    except Exception as exc:
                        print(f"[CAPTION_REPLACE] status update failed: {exc}")
                if EDIT_DELAY:
                    await asyncio.sleep(EDIT_DELAY)

    await asyncio.gather(*(worker() for _ in range(WORKERS)))


async def _send_report(status_message, job, key, label):
    ids = job.get(key) or []
    if not ids:
        return
    path = None
    try:
        fd, path = tempfile.mkstemp(prefix="caption-replace-", suffix=".txt")
        os.close(fd)
        username = job.get("target_username") or None
        with open(path, "w", encoding="utf-8") as report:
            for message_id in ids:
                report.write((_link(job["chat_id"], message_id, username) or str(message_id)) + "\n")
        await _flood(
            lambda: status_message.reply_document(path, caption=f"{label}: {len(ids)}"),
            f"send {label} report",
        )
    except Exception as exc:
        print(f"[CAPTION_REPLACE] {label} report failed: {exc}")
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


async def _run_job(job, status_message):
    global _RUNTIME
    retry = str(job.get("mode", "")).lower() == "retry"
    runtime = {
        "pause": asyncio.Event(),
        "stop": asyncio.Event(),
        "job": job,
    }
    runtime["pause"].set()
    _RUNTIME = runtime

    try:
        if retry:
            ids = [int(x) for x in job.get("retry_ids") or []]
            while int(job.get("retry_index", 0)) < len(ids) and not runtime["stop"].is_set():
                await runtime["pause"].wait()
                index = int(job.get("retry_index", 0))
                await _process_batch(
                    job, ids[index:index + FETCH_BATCH], runtime, status_message, retry=True
                )
        else:
            ids = _range_ids(job)
            while int(job.get("next_index", 0)) < len(ids) and not runtime["stop"].is_set():
                await runtime["pause"].wait()
                index = int(job.get("next_index", 0))
                await _process_batch(
                    job, ids[index:index + FETCH_BATCH], runtime, status_message, retry=False
                )

        if runtime["stop"].is_set():
            job["status"] = "failed" if job.get("fatal_error") else "stopped"
        else:
            job["status"] = "completed"
    except Exception as exc:
        job["status"] = "failed"
        job["fatal_error"] = str(exc)
        print(f"[CAPTION_REPLACE] job failed: {exc}")
    finally:
        await _set_job(job)
        try:
            await _flood(
                lambda: status_message.edit_text(
                    _status_text(job), reply_markup=_keyboard(job, active=False)
                ),
                "final status",
            )
        except Exception as exc:
            print(f"[CAPTION_REPLACE] final status update failed: {exc}")
        await _send_report(status_message, job, "skipped_ids", "SKIPPED MSG LINKS")
        await _send_report(status_message, job, "failed_ids", "FAILED MSG LINKS")
        _RUNTIME = None


def _start_task(job, status_message):
    global _JOB_TASK
    _JOB_TASK = asyncio.create_task(_run_job(job, status_message))

    def clear_task(_):
        global _JOB_TASK
        _JOB_TASK = None

    _JOB_TASK.add_done_callback(clear_task)


def _session_start(user_id):
    _SESSIONS[int(user_id)] = {"step": "target"}
    return _SESSIONS[int(user_id)]


async def _show_saved(message):
    job = await _get_job()
    if not job:
        await message.reply_text("No saved Caption Replace job. Run /caption_replace to create one.")
        return
    if str(job.get("status", "")).lower() == "stopping":
        job["status"] = "stopped"
        await _set_job(job)
    await message.reply_text(
        _status_text(job),
        reply_markup=_keyboard(job, active=_JOB_TASK is not None and not _JOB_TASK.done()),
    )


@Bot.on_message(filters.private & filters.command("caption_replace"), group=-3)
async def caption_replace_command(_, message):
    if not _owner(message.from_user.id):
        await message.reply_text("⛔ Owner/Admin Only")
        raise StopPropagation

    job = await _get_job()
    if job and str(job.get("status", "")).lower() in {
        "running", "paused", "stopping", "stopped", "failed", "completed"
    }:
        await _show_saved(message)
        raise StopPropagation

    _session_start(message.from_user.id)
    await message.reply_text(
        "**Caption Replace — New Job**\n\n"
        "Send the numeric target channel ID.\n"
        "The channel must be explicitly listed in CAPTION_REPLACE_ALLOWED_CHANNELS."
    )
    raise StopPropagation


@Bot.on_message(
    filters.private & filters.text & ~filters.command(bot_legacy.FILESTORE_COMMANDS),
    group=-3,
)
async def caption_replace_input(_, message):
    user_id = int(message.from_user.id)
    session = _SESSIONS.get(user_id)
    if not session:
        return
    if not _owner(user_id):
        _SESSIONS.pop(user_id, None)
        await message.reply_text("⛔ Owner/Admin Only")
        raise StopPropagation

    value = message.text
    try:
        step = session["step"]

        if value.strip().lower() == "/cancel":
            _SESSIONS.pop(user_id, None)
            await message.reply_text("✅ Caption Replace setup cancelled.")
            raise StopPropagation

        if step == "target":
            raw = value.strip()
            if not raw.lstrip("-").isdigit():
                raise ValueError("Target channel ID must be numeric, for example -1001234567890.")
            chat_id = int(raw)
            chat = await _resolve_target(chat_id)
            session.update(
                step="start",
                chat_id=chat_id,
                chat=chat,
                target_title=getattr(chat, "title", "") or "",
            )
            await message.reply_text("✅ Target accepted. Send the START message ID.")
        elif step == "start":
            if not value.strip().isdigit() or int(value) <= 0:
                raise ValueError("Message ID must be a positive number.")
            session.update(step="end", start_id=int(value))
            await message.reply_text("Send the STOP / END message ID.")
        elif step == "end":
            if not value.strip().isdigit() or int(value) <= 0:
                raise ValueError("Message ID must be a positive number.")
            session["end_id"] = int(value)
            if abs(session["end_id"] - session["start_id"]) + 1 > 200000:
                raise ValueError("Keep one maintenance range at or below 200,000 messages.")
            session["step"] = "find"
            await message.reply_text(
                "Send the exact FIND text. Matching is literal and case-sensitive; "
                "spaces and newlines are preserved."
            )
        elif step == "find":
            if not value or len(value) > 1024:
                raise ValueError("FIND text must be 1–1024 characters.")
            session.update(step="replace", find=value)
            await message.reply_text("Send the REPLACE text.")
        elif step == "replace":
            if not value or len(value) > 1024:
                raise ValueError("REPLACE text must be 1–1024 characters.")
            session.update(step="review", replace=value)
            await message.reply_text(
                _review(session),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Start", callback_data="cr:start"),
                        InlineKeyboardButton("🧪 Dry Run", callback_data="cr:dry"),
                    ],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cr:cancel")],
                ]),
            )
        else:
            await message.reply_text("Use the confirmation buttons or /cancel.")
    except StopPropagation:
        raise
    except Exception as exc:
        await message.reply_text(f"❌ {exc}")
    raise StopPropagation


async def _create_job_from_session(user_id, dry_run=False):
    session = _SESSIONS.get(int(user_id))
    if not session or session.get("step") != "review":
        raise ValueError("The setup session expired. Run /caption_replace again.")
    if _JOB_TASK is not None and not _JOB_TASK.done():
        raise ValueError("Another Caption Replace job is already running.")

    chat = await _resolve_target(int(session["chat_id"]))
    session["chat"] = chat

    storage_channel = await db.get_db_channel_id()
    if storage_channel is not None and int(storage_channel) == int(session["chat_id"]):
        raise RuntimeError("STORAGE_CONFIRM_REQUIRED")

    job = _make_job(session, dry_run=dry_run)
    _SESSIONS.pop(int(user_id), None)
    await _set_job(job)
    return job


@Bot.on_callback_query(filters.regex(r"^cr:"), group=-3)
async def caption_replace_callback(_, query):
    global _RUNTIME
    user_id = int(query.from_user.id)

    if not _owner(user_id):
        await query.answer("Owner/Admin Only", show_alert=True)
        raise StopPropagation

    action = str(query.data).split(":", 1)[1]
    try:
        await query.answer()
    except Exception:
        pass

    try:
        if action == "cancel":
            _SESSIONS.pop(user_id, None)
            await query.message.edit_text("✅ Caption Replace setup cancelled.")
            raise StopPropagation

        if action == "new":
            if _JOB_TASK is not None and not _JOB_TASK.done():
                raise ValueError("Stop the current job first.")
            _session_start(user_id)
            await query.message.reply_text(
                "**Caption Replace — New Job**\n\nSend the numeric target channel ID."
            )
            raise StopPropagation

        if action.startswith("confirm:"):
            session = _SESSIONS.get(user_id)
            if not session:
                raise ValueError("Setup session expired.")
            await _resolve_target(int(session["chat_id"]))
            job = _make_job(session, dry_run=action.endswith(":dry"))
            _SESSIONS.pop(user_id, None)
            await _set_job(job)
            await query.message.edit_text(
                _status_text(job), reply_markup=_keyboard(job, active=True)
            )
            _start_task(job, query.message)
            raise StopPropagation

        job = _RUNTIME["job"] if _RUNTIME is not None else await _get_job()
        if not job:
            raise ValueError("No saved Caption Replace job found.")

        state = str(job.get("status", "")).lower()

        if action == "status":
            await query.message.edit_text(
                _status_text(job),
                reply_markup=_keyboard(
                    job, active=_JOB_TASK is not None and not _JOB_TASK.done()
                ),
            )
            raise StopPropagation

        if action == "pause":
            if _RUNTIME is None or _JOB_TASK is None or _JOB_TASK.done():
                raise ValueError("Worker is not active. Use Resume.")
            _RUNTIME["pause"].clear()
            _RUNTIME["job"]["status"] = "paused"
            await _set_job(_RUNTIME["job"])
            await query.message.edit_text(
                _status_text(_RUNTIME["job"]),
                reply_markup=_keyboard(_RUNTIME["job"], active=False),
            )
            raise StopPropagation

        if action == "resume":
            if _JOB_TASK is not None and not _JOB_TASK.done():
                if _RUNTIME is not None:
                    _RUNTIME["pause"].set()
                    _RUNTIME["job"]["status"] = "running"
                    await _set_job(_RUNTIME["job"])
                    await query.message.edit_text(
                        _status_text(_RUNTIME["job"]),
                        reply_markup=_keyboard(_RUNTIME["job"], active=True),
                    )
                raise StopPropagation

            if state not in {"running", "paused", "stopped", "failed"}:
                raise ValueError("This job is already completed. Start a new job or retry failed IDs.")
            await _resolve_target(int(job["chat_id"]))
            job["status"] = "running"
            job["fatal_error"] = ""
            await _set_job(job)
            await query.message.edit_text(
                _status_text(job), reply_markup=_keyboard(job, active=True)
            )
            _start_task(job, query.message)
            raise StopPropagation

        if action == "stop":
            if _RUNTIME is not None and _JOB_TASK is not None and not _JOB_TASK.done():
                _RUNTIME["stop"].set()
                _RUNTIME["job"]["status"] = "stopping"
                await _set_job(_RUNTIME["job"])
                await query.message.edit_text(
                    _status_text(_RUNTIME["job"]),
                    reply_markup=_keyboard(_RUNTIME["job"], active=False),
                )
            else:
                job["status"] = "stopped"
                await _set_job(job)
                await query.message.edit_text(
                    _status_text(job), reply_markup=_keyboard(job, active=False)
                )
            raise StopPropagation

        if action == "retry":
            if _JOB_TASK is not None and not _JOB_TASK.done():
                raise ValueError("Another job is already running.")
            failed = [int(x) for x in job.get("failed_ids") or []]
            if not failed:
                raise ValueError("There are no failed IDs to retry.")
            await _resolve_target(int(job["chat_id"]))
            retry_job = copy.deepcopy(job)
            retry_job.update(
                mode="retry",
                status="running",
                retry_ids=failed,
                retry_index=0,
                retry_total=len(failed),
                last_error="",
                fatal_error="",
            )
            await _set_job(retry_job)
            await query.message.edit_text(
                _status_text(retry_job), reply_markup=_keyboard(retry_job, active=True)
            )
            _start_task(retry_job, query.message)
            raise StopPropagation

        if action in {"start", "dry"}:
            session = _SESSIONS.get(user_id)
            if not session or session.get("step") != "review":
                raise ValueError("The setup session expired. Run /caption_replace again.")
            if job and state in {"running", "paused", "stopping"}:
                raise ValueError("A saved Caption Replace job is already active. Use Resume/Status first.")

            try:
                new_job = await _create_job_from_session(
                    user_id, dry_run=(action == "dry")
                )
            except RuntimeError as exc:
                if str(exc) != "STORAGE_CONFIRM_REQUIRED":
                    raise
                await query.message.edit_text(
                    _review(session)
                    + "\n\n⚠️ This is the configured HJ storage channel. "
                    "It is permitted only because it was explicitly allow-listed. "
                    "Confirm this separate maintenance edit.",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "⚠️ Confirm Storage Edit",
                                callback_data=f"cr:confirm:{action}",
                            ),
                            InlineKeyboardButton("❌ Cancel", callback_data="cr:cancel"),
                        ]
                    ]),
                )
                raise StopPropagation

            await query.message.edit_text(
                _status_text(new_job), reply_markup=_keyboard(new_job, active=True)
            )
            _start_task(new_job, query.message)
            raise StopPropagation

        raise ValueError("Unknown action.")
    except StopPropagation:
        raise
    except Exception as exc:
        await query.answer(str(exc)[:190], show_alert=True)
    raise StopPropagation
