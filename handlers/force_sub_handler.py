# HJ GROUPS OF FILES - Force Subscribe disabled

from pyrogram import Client
from pyrogram.types import Message


async def handle_force_sub(bot: Client, cmd: Message, user_id=None):
    """Force Subscribe is intentionally disabled for HJ GROUPS OF FILES."""
    return 200
