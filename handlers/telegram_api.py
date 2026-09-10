# HJ GROUPS OF FILES - Telegram Bot API helpers
# Used as a fallback for private-channel operations when a fresh Pyrogram
# session has not yet cached the channel peer.

import asyncio
import requests
from configs import Config


_API_BASE = f"https://api.telegram.org/bot{Config.BOT_TOKEN}"


def _call(method: str, payload: dict):
    response = requests.post(
        f"{_API_BASE}/{method}",
        json=payload,
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"Telegram Bot API error in {method}")
    return data.get("result")


async def copy_message(
    chat_id: int,
    from_chat_id: int,
    message_id: int,
    protect_content: bool = False,
    caption=None,
    reply_markup=None,
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
    return await asyncio.to_thread(_call, "copyMessage", payload)


async def edit_message_caption(chat_id: int, message_id: int, caption: str):
    return await asyncio.to_thread(
        _call,
        "editMessageCaption",
        {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "caption": str(caption),
        },
    )


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
