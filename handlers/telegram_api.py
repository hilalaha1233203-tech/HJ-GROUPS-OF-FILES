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


async def copy_message(chat_id: int, from_chat_id: int, message_id: int, protect_content: bool = False):
    return await asyncio.to_thread(
        _call,
        "copyMessage",
        {
            "chat_id": int(chat_id),
            "from_chat_id": int(from_chat_id),
            "message_id": int(message_id),
            "protect_content": bool(protect_content),
        },
    )


async def delete_message(chat_id: int, message_id: int):
    return await asyncio.to_thread(
        _call,
        "deleteMessage",
        {"chat_id": int(chat_id), "message_id": int(message_id)},
    )
