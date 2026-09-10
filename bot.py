import bot_legacy
import enhancements
from pyrogram import filters
from pyrogram.types import Message, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from configs import Config
from handlers.database import db
from handlers.save_media import save_media_in_channel, save_batch_media_in_channel
Bot = bot_legacy.Bot
USER_WORKFLOW = {}
FILESTORE_COMMANDS = list(bot_legacy.FILESTORE_COMMANDS)
def _state(uid): return USER_WORKFLOW.setdefault(str(int(uid)), {"recent": []})
def _origin(m):
    if not m: return None
    o=getattr(m,"forward_origin",None)
    if o and getattr(o,"chat",None) and getattr(o,"message_id",None): return int(o.chat.id),int(o.message_id)
    c=getattr(m,"forward_from_chat",None); mid=getattr(m,"forward_from_message_id",None)
    return (int(c.id),int(mid)) if c and mid else None
def _remember(m):
    o=_origin(m)
    if not o or not m.from_user:return
    s=_state(m.from_user.id); s["recent"]=[x for x in s["recent"] if (x["source_chat_id"],x["source_message_id"])!=(o[0],o[1])][-99:]; s["recent"].append({"source_chat_id":o[0],"source_message_id":o[1],"bot_message_id":m.id})
def _latest(uid):
    r=_state(uid)["recent"]
    if not r:return []
    c=r[-1]["source_chat_id"]
    return [x for x in r if x["source_chat_id"]==c]
def _clear(uid): USER_WORKFLOW[str(int(uid))]={"recent":[]}
@Bot.on_message(filters.private & (filters.document|filters.video|filters.audio|filters.photo|filters.animation) & ~filters.command(FILESTORE_COMMANDS),group=-1)
async def remember_media(_,m): _remember(m)
@Bot.on_message(filters.private & filters.text & ~filters.command(FILESTORE_COMMANDS),group=-1)
async def remember_text(_,m): _remember(m)
async def _last(bot,m):
    r=_state(m.from_user.id)["recent"]
    if not r:return None
    try:return await bot.get_messages(m.chat.id,r[-1]["bot_message_id"])
    except Exception:return None
@Bot.on_message(filters.private & filters.command("genlink"),group=-1)
async def genlink(bot,m):
    target=m.reply_to_message or await _last(bot,m)
    if not target: await m.reply_text("📎 First forward the file/message from your channel to this bot.\n\nThen send `/genlink`."); return
    status=await m.reply_text("⏳ Generating your permanent link..."); await save_media_in_channel(bot,status,target); _clear(m.from_user.id)
async def make_batch(bot,m,items):
    if len(items)<2: await m.reply_text("📦 Forward the FIRST and LAST messages from the same channel, then send `/batch`."); return
    a,b=items[-2],items[-1]
    if a["source_chat_id"]!=b["source_chat_id"]: await m.reply_text("❌ FIRST and LAST messages must be from the same channel."); return
    lo,hi=sorted((int(a["source_message_id"]),int(b["source_message_id"]))); status=await m.reply_text(f"⏳ Creating one batch link for `{hi-lo+1}` messages..."); await save_batch_media_in_channel(bot,status,list(range(lo,hi+1)),int(a["source_chat_id"]),int(m.from_user.id)); _clear(m.from_user.id)
@Bot.on_message(filters.private & filters.command("batch"),group=-1)
async def batch(bot,m):
    if len(m.command)>=3:
        try:a,b=int(m.command[1]),int(m.command[2])
        except ValueError: await m.reply_text("❌ Message IDs must be numbers."); return
        r=_state(m.from_user.id)["recent"]; c=_origin(m.reply_to_message)[0] if _origin(m.reply_to_message) else (r[-1]["source_chat_id"] if r else None)
        if c is None: await m.reply_text("📎 Forward one source message first."); return
        lo,hi=sorted((a,b)); status=await m.reply_text(f"⏳ Creating one batch link for `{hi-lo+1}` messages..."); await save_batch_media_in_channel(bot,status,list(range(lo,hi+1)),int(c),int(m.from_user.id)); _clear(m.from_user.id); return
    await make_batch(bot,m,_latest(m.from_user.id))
@Bot.on_message(filters.private & filters.command("custom_batch"),group=-1)
async def custom_batch(bot,m):
    selected=_latest(m.from_user.id)
    if not selected: await m.reply_text("📦 Forward the messages you want in the batch, then send `/custom_batch`."); return
    c=selected[-1]["source_chat_id"]; ids=[x["source_message_id"] for x in selected]; status=await m.reply_text(f"⏳ Creating one batch link for `{len(ids)}` selected messages..."); await save_batch_media_in_channel(bot,status,ids,c,int(m.from_user.id)); _clear(m.from_user.id)
@Bot.on_message(filters.private & filters.command("special_link"),group=-1)
async def special(bot,m): await make_batch(bot,m,_latest(m.from_user.id))
@Bot.on_message(filters.private & filters.command("universal_link"),group=-1)
async def universal(bot,m): await make_batch(bot,m,_latest(m.from_user.id))
@Bot.on_message(filters.private & filters.command("clear_batch"),group=-1)
async def clear(_,m): _clear(m.from_user.id); bot_legacy.MediaList[str(m.from_user.id)]=[]; await m.reply_text("✅ Cleared your batch selection successfully!")
async def setup_bot_commands():
    await Bot.set_bot_commands([BotCommand("start","Start / open file links"),BotCommand("genlink","Generate a single link"),BotCommand("batch","Generate a batch link"),BotCommand("custom_batch","Generate selected batch"),BotCommand("shortener","Shorten a link"),BotCommand("settings","Customize settings"),BotCommand("clear_batch","Clear batch"),BotCommand("special_link","Create a batch link"),BotCommand("universal_link","Create a batch link"),BotCommand("broadcast","Broadcast"),BotCommand("status","Status"),BotCommand("ban_user","Ban user"),BotCommand("unban_user","Unban user"),BotCommand("banned_users","Banned users"),BotCommand("direct","Owner direct link"),BotCommand("direct_batch","Owner direct batch")])
bot_legacy.setup_bot_commands=setup_bot_commands
run_bot=bot_legacy.run_bot
if __name__=="__main__": Bot.run(run_bot())
