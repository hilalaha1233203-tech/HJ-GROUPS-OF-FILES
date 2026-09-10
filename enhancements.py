# HJ GROUPS STORE KEEPER - dynamic settings, captions/buttons and owner-direct links
import asyncio
import base64
import json
import re
import secrets
import requests
from pyrogram import filters, StopPropagation
from pyrogram.errors import PeerIdInvalid
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import bot_legacy
from configs import Config
from handlers.database import db
from handlers import save_media, send_file
Bot = bot_legacy.Bot
OWNER = int(Config.BOT_OWNER or 0)
STATE = {}
SHORTENER_CACHE = {"url": Config.SHORTLINK_URL or "", "api": Config.SHORTLINK_API or ""}
BUTTON_CACHE = {"main": Config.MAIN_CHANNEL, "pocket": Config.POCKET_LIBRARY, "backup": Config.BACKUP_CHANNEL}

def _owner(uid):
    return bool(OWNER) and int(uid) == OWNER

def _st(uid): return STATE.setdefault(int(uid), {})
def _clear(uid): STATE.pop(int(uid), None)
async def _get(key, default=""):
    try: return await db._get_setting(key, default)
    except Exception: return default
async def _set(key, value): await db._set_setting(key, value)

async def load_settings():
    url = str(await _get("shortener_url", "") or "").strip().rstrip("/")
    api = str(await _get("shortener_api", "") or "").strip()
    if not url and Config.SHORTLINK_URL:
        url = Config.SHORTLINK_URL; await _set("shortener_url", url)
    if not api and Config.SHORTLINK_API:
        api = Config.SHORTLINK_API; await _set("shortener_api", api)
    SHORTENER_CACHE.update(url=url, api=api); Config.SHORTLINK_URL = url or None; Config.SHORTLINK_API = api or None
    raw = await _get("custom_buttons", "{}")
    try: saved = json.loads(raw or "{}"); saved = saved if isinstance(saved, dict) else {}
    except Exception: saved = {}
    changed = False
    for k, v in BUTTON_CACHE.items():
        if k not in saved and v: saved[k] = v; changed = True
    BUTTON_CACHE.update({k: saved.get(k) for k in BUTTON_CACHE})
    if changed or not raw: await _set("custom_buttons", json.dumps(BUTTON_CACHE, ensure_ascii=False, separators=(",", ":")))

def _short_sync(url):
    if not SHORTENER_CACHE["url"] or not SHORTENER_CACHE["api"]: return url
    try:
        r = requests.get(f"https://{SHORTENER_CACHE['url']}/api", params={"api": SHORTENER_CACHE["api"], "url": url}, timeout=10)
        return r.json().get("shortenedUrl") or url
    except Exception as exc: print(f"[SHORTENER] {exc}"); return url
async def shorten(url): return await asyncio.to_thread(_short_sync, url)
bot_legacy.get_short = _short_sync
save_media.get_short = _short_sync

def _url(v):
    v = str(v or "").strip()
    if re.match(r"^https?://", v, re.I): return v
    if v.startswith("@"): return f"https://t.me/{v[1:]}"
    return None

def _buttons_markup():
    rows=[]
    for k,l in (("main","Main Channel"),("pocket","Pocket Library"),("backup","Backup Channel")):
        u=_url(BUTTON_CACHE.get(k))
        if u: rows.append([InlineKeyboardButton(l,url=u)])
    return InlineKeyboardMarkup(rows) if rows else None

def _caption_text(message, template):
    if not template: return None
    try: name,size=send_file._file_meta(message); size=send_file.human_size(size)
    except Exception: name,size="Telegram Media","Unknown"
    original=str(getattr(message,"caption",None) or getattr(message,"text",None) or "").strip(); user=getattr(message,"from_user",None)
    vals={"file_name":name or "Telegram Media","file_size":size,"caption":original,"username":getattr(user,"username","") if user else "","user_id":str(getattr(user,"id","") or "") if user else "","first_name":getattr(user,"first_name","") if user else ""}
    for k,v in vals.items(): template=template.replace("{"+k+"}",str(v))
    return template[:1024]
async def _caption(message): return _caption_text(message, await _get("custom_caption", ""))

async def _ensure_peer(bot, chat_id):
    """Make sure a Telegram peer is present in Pyrogram's cache after restarts."""
    chat_id = int(chat_id)
    try:
        await bot.resolve_peer(chat_id)
        return chat_id
    except PeerIdInvalid:
        pass
    except Exception:
        pass

    # This bot uses an in-memory Pyrogram session. After a restart/redeploy the
    # peer cache is empty. get_dialogs() refreshes Telegram peers and stores the
    # channel access_hash required by get_messages/copy_message.
    try:
        async for dialog in bot.get_dialogs():
            chat = getattr(dialog, "chat", None)
            if chat is not None and int(chat.id) == chat_id:
                await bot.resolve_peer(chat_id)
                return chat_id
    except Exception as exc:
        raise RuntimeError(f"Could not refresh Telegram peers: {exc}") from exc

    raise RuntimeError(
        f"Telegram source channel {chat_id} is not available to this bot session. "
        "Make sure the bot is still a member/admin of that private channel."
    )

async def enhanced_media_forward(bot,user_id,file_id,channel_id=None):
    channel_id=channel_id or await db.get_db_channel_id()
    if channel_id is None: raise RuntimeError("Storage channel is not configured.")
    try: source=await bot.get_messages(channel_id,file_id)
    except Exception: source=None
    cap=await _caption(source) if source else None
    try:
        return await bot.copy_message(chat_id=user_id,from_chat_id=channel_id,message_id=file_id,caption=cap,protect_content=await db.get_protect_content(),reply_markup=_buttons_markup())
    except Exception as exc:
        from handlers.telegram_api import copy_message, edit_message_caption, edit_message_reply_markup
        copied=await copy_message(chat_id=user_id,from_chat_id=channel_id,message_id=file_id,protect_content=await db.get_protect_content())
        cid=copied.get("message_id") if isinstance(copied,dict) else getattr(copied,"id",None)
        if cid and cap:
            try: await edit_message_caption(user_id,cid,cap)
            except Exception: pass
        kb={"inline_keyboard":[[{"text":l,"url":_url(BUTTON_CACHE.get(k))}] for k,l in (("main","Main Channel"),("pocket","Pocket Library"),("backup","Backup Channel")) if _url(BUTTON_CACHE.get(k))]}
        if cid and kb["inline_keyboard"]:
            try: await edit_message_reply_markup(user_id,cid,kb)
            except Exception: pass
        return copied
send_file.media_forward=enhanced_media_forward

def _link(prefix,value): return f"https://telegram.me/{Config.BOT_USERNAME}?start={prefix}{base64.urlsafe_b64encode(value.encode()).decode().rstrip('=')}"
async def _direct_single(chat_id,mid):
    t=secrets.token_urlsafe(12); await _set(f"direct:{t}",json.dumps({"chat_id":int(chat_id),"message_id":int(mid)},separators=(",",":"))); return _link("HJDirect_",t)
async def _direct_batch(items):
    t=secrets.token_urlsafe(12); await _set(f"direct_batch:{t}",json.dumps([{"chat_id":int(c),"message_id":int(m)} for c,m in items],separators=(",",":"))); return _link("HJDirectBatch_",t)
async def _origin(message):
    if not message:return None
    o=getattr(message,"forward_origin",None)
    if o and getattr(o,"chat",None) and getattr(o,"message_id",None): return int(o.chat.id),int(o.message_id)
    c,mid=getattr(message,"forward_from_chat",None),getattr(message,"forward_from_message_id",None)
    return (int(c.id),int(mid)) if c is not None and mid else None

async def enhanced_save_single(bot,editable,message):
    requester=int(editable.from_user.id) if editable.from_user else 0; origin=await _origin(message)
    if requester==OWNER and origin:
        link=await _direct_single(*origin); await editable.edit(f"**Direct Channel Link Generated!**\n\n{await shorten(link)}\n\nThe source file was not copied to the Database Channel.",disable_web_page_preview=True); return
    try:
        channel_id=await db.get_db_channel_id()
        if channel_id is None: raise RuntimeError("Storage channel is not configured.")
        sent=await bot.copy_message(chat_id=channel_id,from_chat_id=message.chat.id,message_id=message.id,caption=await _caption(message),protect_content=await db.get_protect_content(),reply_markup=_buttons_markup())
        sid=getattr(sent,"id",None) or (sent.get("message_id") if isinstance(sent,dict) else None)
        if not sid: raise RuntimeError("Could not determine stored message ID")
        link=_link("HJGroups_",f"{channel_id}|{sid}"); await editable.edit(f"**Your File Stored in my Database!**\n\nHere is the Permanent Link of your file: {await shorten(link)}\n\nJust Click the link to get your file!",disable_web_page_preview=True)
    except Exception as exc: await editable.edit(f"Something Went Wrong!\n\n**Error:** `{exc}`")

async def enhanced_save_batch(bot,editable,message_ids,source_chat_id=None,request_user_id=None):
    requester=int(request_user_id or (editable.from_user.id if editable.from_user else 0))
    if requester==OWNER and source_chat_id is not None:
        if len(message_ids)>1001: await editable.edit("❌ Direct batch cannot exceed 1001 messages."); return
        link=await _direct_batch([(int(source_chat_id),int(mid)) for mid in message_ids]); await editable.edit(f"**Direct Channel Batch Link Generated!**\n\n{await shorten(link)}\n\nTotal Messages: `{len(message_ids)}`\nNo files were copied to the Database Channel.",disable_web_page_preview=True); return
    try:
        channel_id=await db.get_db_channel_id(); source_chat_id=int(source_chat_id or editable.chat.id)
        if channel_id is None: raise RuntimeError("Storage channel is not configured.")
        saved=[]
        for mid in message_ids:
            src=await bot.get_messages(source_chat_id,int(mid)); sent=await bot.copy_message(chat_id=channel_id,from_chat_id=source_chat_id,message_id=int(mid),caption=await _caption(src),protect_content=await db.get_protect_content(),reply_markup=_buttons_markup()); sid=getattr(sent,"id",None) or (sent.get("message_id") if isinstance(sent,dict) else None)
            if sid:saved.append(int(sid))
        if not saved: raise RuntimeError("No files were available to save in this batch.")
        idx=await bot.send_message(channel_id," ".join(map(str,saved)),disable_web_page_preview=True); link=_link("HJGroups_",f"{channel_id}|{idx.id}")
        await editable.edit(f"**Batch Files Stored in my Database!**\n\nHere is the Permanent Link of your files: {await shorten(link)}\n\nJust Click the link to get your files!",disable_web_page_preview=True)
    except Exception as exc: await editable.edit(f"Something Went Wrong!\n\n**Error:** `{exc}`")
save_media.save_media_in_channel=enhanced_save_single
save_media.save_batch_media_in_channel=enhanced_save_batch

async def show_settings(msg):
    await msg.reply_text("**Settings**\nCustomize your settings as your need",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 URL SHORTENER",callback_data="hjset_shortener")],[InlineKeyboardButton("✏️ CUSTOM CAPTION",callback_data="hjset_caption")],[InlineKeyboardButton("🔘 CUSTOM BUTTON",callback_data="hjset_button")],[InlineKeyboardButton("🛡 PROTECT CONTENT",callback_data="hjset_protect")],[InlineKeyboardButton("< BACK",callback_data="hjset_main")]]))
@Bot.on_message(filters.private & filters.command("settings"),group=-2)
async def settings_cmd(_,m):
    if not _owner(m.from_user.id): await m.reply_text("⛔ Owner/Admin Only"); raise StopPropagation
    await show_settings(m); raise StopPropagation

async def short_page(msg):
    u,a=SHORTENER_CACHE["url"],SHORTENER_CACHE["api"]
    if u and a: text=f"**Link Shortener**\n- Shortener:\n`{u}`\n- Shortener Api:\n`{a}`\n\nYou can now use the /shortener command to shorten any\nlinks."; kb=[[InlineKeyboardButton("Delete shortener",callback_data="hjset_short_delete")],[InlineKeyboardButton("back",callback_data="hjset_main")]]
    else: text="**Link Shortener**\n\nNo shortener configured."; kb=[[InlineKeyboardButton("Add shortener",callback_data="hjset_short_add")],[InlineKeyboardButton("back",callback_data="hjset_main")]]
    await msg.edit_text(text,reply_markup=InlineKeyboardMarkup(kb))
async def caption_page(msg):
    cur=await _get("custom_caption",""); await msg.edit_text("**Custom Caption:**\n\nYou can add a custom caption to your media messages\ninstead of its original caption\n\nFillings:\n• {file_name} : File Name\n• {file_size} : File size\n• {caption} : Orginal Caption\n\n**Current:**\n"+(cur or "Not configured"),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Edit",callback_data="hjset_caption_edit")],[InlineKeyboardButton("Delete",callback_data="hjset_caption_delete")],[InlineKeyboardButton("See",callback_data="hjset_caption_see")],[InlineKeyboardButton("back",callback_data="hjset_main")]]))
async def button_page(msg):
    text="**Custom Button:**\n\nYou can add a custom button to your message\n\n"+"".join(f"**{l}:** `{BUTTON_CACHE.get(k) or 'Not configured'}`\n" for k,l in (("main","Main Channel"),("pocket","Pocket Library"),("backup","Backup Channel")))
    await msg.edit_text(text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Main Channel",callback_data="hjset_btn_main")],[InlineKeyboardButton("Pocket Library",callback_data="hjset_btn_pocket")],[InlineKeyboardButton("Backup Channel",callback_data="hjset_btn_backup")],[InlineKeyboardButton("Delete",callback_data="hjset_btn_delete_backup")],[InlineKeyboardButton("back",callback_data="hjset_main")]]))
async def protect_page(msg):
    p=await db.get_protection_settings(); f="ON" if p["protect_forward"] else "OFF"; d="ON" if p["protect_download"] else "OFF"; await msg.edit_text(f"**Protect Content**\n\nForward Restriction: `{f}`\nDownload Restriction: `{d}`",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Forward: {f}",callback_data="hjset_toggle_forward")],[InlineKeyboardButton(f"Download: {d}",callback_data="hjset_toggle_download")],[InlineKeyboardButton("back",callback_data="hjset_main")]]))

@Bot.on_callback_query(filters.regex(r"^hjset_"),group=-2)
async def settings_cb(_,q):
    if not _owner(q.from_user.id): await q.answer("Owner/Admin Only",show_alert=True); raise StopPropagation
    d=q.data; await q.answer()
    if d in {"hjset_main","hjset_back"}: await show_settings(q.message)
    elif d=="hjset_shortener": await short_page(q.message)
    elif d=="hjset_short_add": _st(q.from_user.id).update(mode="shortener"); await q.message.edit_text("Send shortener domain and API on two lines.\n\nExample:\n`arolinks.com`\n`YOUR_API_KEY`\n\nSend /cancel to stop.")
    elif d=="hjset_short_delete": SHORTENER_CACHE.update(url="",api=""); Config.SHORTLINK_URL=Config.SHORTLINK_API=None; await _set("shortener_url",""); await _set("shortener_api",""); await short_page(q.message)
    elif d=="hjset_caption": await caption_page(q.message)
    elif d=="hjset_caption_edit": _st(q.from_user.id).update(mode="caption"); await q.message.edit_text("Send your complete custom caption template.\n\nAvailable:\n`{file_name}`\n`{file_size}`\n`{caption}`\n`{username}`\n`{user_id}`\n`{first_name}`\n\nSend /cancel to stop.")
    elif d=="hjset_caption_delete": await _set("custom_caption",""); await caption_page(q.message)
    elif d=="hjset_caption_see": cur=await _get("custom_caption",""); await q.message.edit_text(f"**Custom Caption Preview**\n\n{cur or 'Not configured'}",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("back",callback_data="hjset_caption")]]))
    elif d=="hjset_button": await button_page(q.message)
    elif d in {"hjset_btn_main","hjset_btn_pocket","hjset_btn_backup"}: k=d.rsplit("_",1)[1]; _st(q.from_user.id).update(mode="button",button=k); l={"main":"Main Channel","pocket":"Pocket Library","backup":"Backup Channel"}[k]; await q.message.edit_text(f"Send the Telegram channel/group link or @username for **{l}**.\n\nSend /cancel to stop.")
    elif d=="hjset_btn_delete_backup": BUTTON_CACHE["backup"]=None; await _set("custom_buttons",json.dumps(BUTTON_CACHE,ensure_ascii=False,separators=(",",":"))); await button_page(q.message)
    elif d=="hjset_protect": await protect_page(q.message)
    elif d in {"hjset_toggle_forward","hjset_toggle_download"}: k="protect_forward" if d.endswith("forward") else "protect_download"; p=await db.get_protection_settings(); await db.set_protection_setting(k,not p[k]); await protect_page(q.message)
    raise StopPropagation

@Bot.on_message(filters.private & filters.text & ~filters.command(bot_legacy.FILESTORE_COMMANDS),group=-2)
async def settings_input(_,m):
    if not _owner(m.from_user.id): return
    s=_st(m.from_user.id); mode=s.get("mode")
    if not mode:return
    if m.text.strip().lower()=="/cancel": _clear(m.from_user.id); await m.reply_text("Cancelled."); raise StopPropagation
    if mode=="shortener":
        a=[x.strip() for x in m.text.splitlines() if x.strip()]
        if len(a)!=2: await m.reply_text("Send exactly two lines: shortener domain, then API key."); raise StopPropagation
        domain=re.sub(r"^https?://","",a[0],flags=re.I).rstrip("/")
        if not re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",domain): await m.reply_text("Invalid shortener domain."); raise StopPropagation
        SHORTENER_CACHE.update(url=domain,api=a[1]); Config.SHORTLINK_URL,Config.SHORTLINK_API=domain,a[1]; await _set("shortener_url",domain); await _set("shortener_api",a[1]); _clear(m.from_user.id); await m.reply_text("✅ Shortener saved.")
    elif mode=="caption":
        v=m.text.strip()
        if not v or len(v)>1024: await m.reply_text("Custom caption must be 1–1024 characters."); raise StopPropagation
        await _set("custom_caption",v); _clear(m.from_user.id); await m.reply_text("✅ Custom caption saved.")
    elif mode=="button":
        v=m.text.strip()
        if not _url(v): await m.reply_text("Send a valid Telegram link or @username."); raise StopPropagation
        BUTTON_CACHE[s["button"]]=v; await _set("custom_buttons",json.dumps(BUTTON_CACHE,ensure_ascii=False,separators=(",",":"))); _clear(m.from_user.id); await m.reply_text("✅ Button saved.")
    raise StopPropagation

@Bot.on_message(filters.private & filters.command("direct"),group=-2)
async def direct_cmd(bot,m):
    if not _owner(m.from_user.id): await m.reply_text("⛔ Owner/Admin Only"); raise StopPropagation
    try:
        ref=await _origin(m.reply_to_message); a=m.command[1:]
        if ref: chat_id,mid=ref
        elif len(a)==2: chat_id=int(a[0]) if a[0].lstrip("-").isdigit() else int((await bot.get_chat(a[0])).id); mid=int(a[1])
        else: raise ValueError("Use /direct <channel> <message_id> or reply to a forwarded channel message.")
        await _ensure_peer(bot,chat_id); await m.reply_text(f"✅ **Direct Link Generated**\n\n{await shorten(await _direct_single(chat_id,mid))}",disable_web_page_preview=True)
    except Exception as exc: await m.reply_text(f"❌ `{exc}`")
    raise StopPropagation

@Bot.on_message(filters.private & filters.command("direct_batch"),group=-2)
async def direct_batch_cmd(bot,m):
    if not _owner(m.from_user.id): await m.reply_text("⛔ Owner/Admin Only"); raise StopPropagation
    try:
        a=m.command[1:]
        if len(a)!=3: raise ValueError("Use /direct_batch <channel> <start_message_id> <end_message_id>")
        chat_id=int(a[0]) if a[0].lstrip("-").isdigit() else int((await bot.get_chat(a[0])).id); start,end=sorted((int(a[1]),int(a[2])))
        if end-start>1000: raise ValueError("Direct batch cannot exceed 1001 messages.")
        await _ensure_peer(bot,chat_id); await m.reply_text(f"✅ **Direct Batch Link Generated**\n\n{await shorten(await _direct_batch([(chat_id,x) for x in range(start,end+1)]))}",disable_web_page_preview=True)
    except Exception as exc: await m.reply_text(f"❌ `{exc}`")
    raise StopPropagation

@Bot.on_message(filters.private & filters.command("start"),group=-2)
async def direct_start(bot,m):
    if len(m.command)<2:return
    p=m.command[1]
    try:
        if p.startswith("HJDirect_"):
            raw=await _get("direct:"+p[len("HJDirect_"):],"")
            if not raw: raise ValueError("This direct link is invalid or no longer available.")
            x=json.loads(raw); await _deliver_direct(bot,m.from_user.id,[(int(x["chat_id"]),int(x["message_id"]))]); raise StopPropagation
        if p.startswith("HJDirectBatch_"):
            raw=await _get("direct_batch:"+p[len("HJDirectBatch_"):],"")
            if not raw: raise ValueError("This direct batch link is invalid or no longer available.")
            x=json.loads(raw); await _deliver_direct(bot,m.from_user.id,[(int(i["chat_id"]),int(i["message_id"])) for i in x]); raise StopPropagation
    except StopPropagation: raise
    except Exception as exc: await m.reply_text(f"❌ Could not open direct link.\n`{exc}`"); raise StopPropagation

async def _deliver_direct(bot,user_id,items):
    ids=[]; protect=await db.get_protect_content(); template=await _get("custom_caption","")
    warmed=set()
    for chat_id,mid in items:
        chat_id=int(chat_id); mid=int(mid)
        if chat_id not in warmed:
            await _ensure_peer(bot,chat_id)
            warmed.add(chat_id)
        source=None
        try:
            source=await bot.get_messages(chat_id,mid)
        except PeerIdInvalid:
            # A fresh worker can lose the peer cache between link creation and
            # delivery. Refresh once and retry before returning an error.
            await _ensure_peer(bot,chat_id)
            source=await bot.get_messages(chat_id,mid)
        except Exception:
            source=None
        try:
            sent=await bot.copy_message(chat_id=user_id,from_chat_id=chat_id,message_id=mid,caption=_caption_text(source,template) if source else None,protect_content=protect,reply_markup=_buttons_markup())
        except PeerIdInvalid:
            await _ensure_peer(bot,chat_id)
            sent=await bot.copy_message(chat_id=user_id,from_chat_id=chat_id,message_id=mid,caption=_caption_text(source,template) if source else None,protect_content=protect,reply_markup=_buttons_markup())
        sid=getattr(sent,"id",None) or (sent.get("message_id") if isinstance(sent,dict) else None)
        if sid:ids.append(int(sid))
    delay=await db.get_auto_delete_seconds()
    if delay>0 and ids:
        notice=await send_file.send_delete_notice(bot,user_id,delay); nid=getattr(notice,"id",None) if notice else None; delete_ids=ids+([int(nid)] if nid else []); task=asyncio.create_task(send_file._delete_delivered_messages(bot,user_id,delete_ids,delay)); send_file._track_delete_task(task)

_original_run_bot=bot_legacy.run_bot
async def _run_bot_enhanced():
    await load_settings(); return await _original_run_bot()
bot_legacy.run_bot=_run_bot_enhanced