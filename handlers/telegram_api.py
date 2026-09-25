# HJ GROUPS OF FILES - Telegram Bot API helpers
# Used as a fallback for private-channel operations when a fresh Pyrogram
# session has not yet cached the channel peer.

import asyncio
import requests
import threading
import time
from collections import deque
from configs import Config


_API_BASE = f"https://api.telegram.org/bot{Config.BOT_TOKEN}"

# Shared Bot API request limiter. Telegram documents about 30 messages/sec for
# free broadcasts; 25 requests/sec leaves a safety margin for this fallback path.
_API_RATE_LIMIT = 25
_API_RATE_WINDOW = 1.0
_API_RATE_LOCK = threading.Lock()
_API_RATE_TIMESTAMPS = deque()


def _wait_for_api_slot():
    while True:
        with _API_RATE_LOCK:
            now = time.monotonic()
            cutoff = now - _API_RATE_WINDOW
            while _API_RATE_TIMESTAMPS and _API_RATE_TIMESTAMPS[0] <= cutoff:
                _API_RATE_TIMESTAMPS.popleft()
            if len(_API_RATE_TIMESTAMPS) < _API_RATE_LIMIT:
                _API_RATE_TIMESTAMPS.append(now)
                return
            wait_for = max(0.01, _API_RATE_TIMESTAMPS[0] + _API_RATE_WINDOW - now)
        time.sleep(wait_for)


def _call(method: str, payload: dict, max_retries: int = 5):
    for attempt in range(max_retries + 1):
        _wait_for_api_slot()
        try:
            response = requests.post(
                f"{_API_BASE}/{method}",
                json=payload,
                timeout=25,
            )
            data = response.json()
        except (requests.RequestException, ValueError):
            if attempt >= max_retries:
                raise
            continue

        if data.get("ok"):
            return data.get("result")

        retry_after = (data.get("parameters") or {}).get("retry_after")
        if retry_after is not None and attempt < max_retries:
            time.sleep(max(1, int(retry_after)))
            continue

        raise RuntimeError(data.get("description") or f"Telegram Bot API error in {method}")


async def copy_messages(
    chat_id: int,
    from_chat_id: int,
    message_ids,
    protect_content: bool = False,
):
    ids = sorted({int(message_id) for message_id in message_ids})
    if not ids:
        return []
    if len(ids) > 100:
        raise ValueError("copy_messages accepts at most 100 message IDs per Telegram Bot API call.")
    return await asyncio.to_thread(
        _call,
        "copyMessages",
        {
            "chat_id": int(chat_id),
            "from_chat_id": int(from_chat_id),
            "message_ids": ids,
            "protect_content": bool(protect_content),
        },
    )


async def copy_message(
    chat_id: int,
    from_chat_id: int,
    message_id: int,
    protect_content: bool = False,
    caption=None,
    reply_markup=None,
    parse_mode=None,
):
    payload = {
        "chat_id": int(chat_id),
        "from_chat_id": int(from_chat_id),
        "message_id": int(message_id),
        "protect_content": bool(protect_content),
    }
    if caption is not None:
        payload["caption"] = str(caption)
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = str(parse_mode)
    return await asyncio.to_thread(_call, "copyMessage", payload)


async def edit_message_caption(chat_id: int, message_id: int, caption: str, parse_mode=None):
    payload = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "caption": str(caption),
    }
    if parse_mode:
        payload["parse_mode"] = str(parse_mode)
    return await asyncio.to_thread(_call, "editMessageCaption", payload)


async def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: dict):
    return await asyncio.to_thread(
        _call,
        "editMessageReplyMarkup",
        {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "reply_markup": reply_markup,
        },
    )


async def get_chat(chat_id):
    """Fetch chat metadata through the Telegram Bot API without Pyrogram peer resolution."""
    return await asyncio.to_thread(
        _call,
        "getChat",
        {"chat_id": int(chat_id)},
    )


async def get_me():
    """Return the bot identity through the Telegram Bot API."""
    return await asyncio.to_thread(_call, "getMe", {})


async def get_chat_member(chat_id, user_id):
    """Fetch a chat member through the Telegram Bot API."""
    return await asyncio.to_thread(
        _call,
        "getChatMember",
        {"chat_id": int(chat_id), "user_id": int(user_id)},
    )


async def send_message(chat_id: int, text: str, **kwargs):
    payload = {"chat_id": int(chat_id), "text": str(text)}
    payload.update(kwargs)
    return await asyncio.to_thread(_call, "sendMessage", payload)


async def delete_message(chat_id: int, message_id: int):
    return await asyncio.to_thread(
        _call,
        "deleteMessage",
        {"chat_id": int(chat_id), "message_id": int(message_id)},
    )

async def delete_messages(chat_id: int, message_ids):
    ids = []
    for value in message_ids or []:
        try:
            mid = int(value)
        except (TypeError, ValueError):
            continue
        if mid > 0 and mid not in ids:
            ids.append(mid)
    if not ids:
        return True
    if len(ids) > 100:
        raise ValueError("delete_messages accepts at most 100 message IDs per Telegram Bot API call.")
    return await asyncio.to_thread(
        _call,
        "deleteMessages",
        {"chat_id": int(chat_id), "message_ids": ids},
    )
