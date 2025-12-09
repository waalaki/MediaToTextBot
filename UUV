import os
import asyncio
import threading
import requests
import logging
import time
import subprocess
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatAction
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
DB_USER = os.environ.get("DB_USER", "lakicalinuur")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "DjReFoWZGbwjry8K")
DB_APPNAME = os.environ.get("DB_APPNAME", "SpeechBot")
MONGO_URI = os.environ.get("MONGO_URI") or f"mongodb+srv://{DB_USER}:{DB_PASSWORD}@cluster0.n4hdlxk.mongodb.net/?retryWrites=true&w=majority&appName={DB_APPNAME}"
FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "ffmpeg")
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "19"))
WINDOW_SECONDS = 24 * 3600
TUTORIAL_CHANNEL = os.environ.get("TUTORIAL_CHANNEL", "@NotifyBchat")
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

LANGS = [
("🇬🇧 English","en"), ("🇸🇦 العربية","ar"), ("🇪🇸 Español","es"), ("🇫🇷 Français","fr"),
("🇷🇺 Русский","ru"), ("🇩🇪 Deutsch","de"), ("🇮🇳 हिन्दी","hi"), ("🇮🇷 فارسی","fa"),
("🇮🇩 Indonesia","id"), ("🇺🇦 Українська","uk"), ("🇦🇿 Azərbaycan","az"), ("🇮🇹 Italiano","it"),
("🇹🇷 Türkçe","tr"), ("🇧🇬 Български","bg"), ("🇷🇸 Srpski","sr"), ("🇵🇰 اردو","ur"),
("🇹🇭 ไทย","th"), ("🇻🇳 Tiếng Việt","vi"), ("🇯🇵 日本語","ja"), ("🇰🇷 한국어","ko"),
("🇨🇳 中文","zh"), ("🇳🇱 Nederlands:nl", "nl"), ("🇸🇪 Svenska","sv"), ("🇳🇴 Norsk","no"),
("🇮🇱 עברית","he"), ("🇩🇰 Dansk","da"), ("🇪🇹 አማርኛ","am"), ("🇫🇮 Suomi","fi"),
("🇧🇩 বাংলা","bn"), ("🇰🇪 Kiswahili","sw"), ("🇪🇹 Oromo","om"), ("🇳🇵 नेपाली","ne"),
("🇵🇱 Polski","pl"), ("🇬🇷 Ελληνικά","el"), ("🇨🇿 Čeština","cs"), ("🇮🇸 Íslenska","is"),
("🇱🇹 Lietuvių","lt"), ("🇱🇻 Latviešu","lv"), ("🇭🇷 Hrvatski","hr"), ("🇷🇸 Bosanski","bs"),
("🇭🇺 Magyar","hu"), ("🇷🇴 Română","ro"), ("🇸🇴 Somali","so"), ("🇲🇾 Melayu","ms"),
("🇺🇿 O'zbekcha","uz"), ("🇵🇭 Tagalog","tl"), ("🇵🇹 Português","pt")
]

user_transcriptions = {}
action_usage = {}
user_keys = {}
user_awaiting_key = {}
lock = threading.Lock()

mongo_client = None
db = None
users_col = None
actions_col = None

def now_ts():
    return int(time.time())

def init_mongo():
    global mongo_client, db, users_col, actions_col, user_keys, action_usage
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        db = mongo_client.get_database(DB_APPNAME or "SpeechBotDB")
        users_col = db.get_collection("users")
        actions_col = db.get_collection("action_usage")
        for doc in users_col.find({}):
            try:
                uid = int(doc["uid"])
                user_keys[uid] = {
                    "key": doc.get("key"),
                    "count": int(doc.get("count", 0)),
                    "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None
                }
            except:
                continue
        for doc in actions_col.find({}):
            k = doc.get("key")
            try:
                c = int(doc.get("count", 0))
            except:
                c = 0
            if k:
                action_usage[k] = c
    except ServerSelectionTimeoutError:
        mongo_client = None
        db = None
        users_col = None
        actions_col = None

init_mongo()

def persist_user_to_db(uid):
    if users_col is None:
        return
    info = user_keys.get(uid)
    if not info:
        users_col.delete_many({"uid": uid})
        return
    users_col.update_one(
        {"uid": uid},
        {"$set": {"uid": uid, "key": info.get("key"), "count": int(info.get("count", 0)), "window_start": info.get("window_start")}},
        upsert=True
    )

def persist_action_usage_to_db(key):
    if actions_col is None:
        return
    cnt = action_usage.get(key, 0)
    actions_col.update_one({"key": key}, {"$set": {"key": key, "count": int(cnt)}}, upsert=True)

def is_gemini_key(key):
    if not key:
        return False
    k = key.strip()
    return k.startswith("AIzaSy")

def store_user_key(uid, key):
    with lock:
        user_keys[uid] = {"key": key.strip(), "count": 0, "window_start": now_ts()}
        user_awaiting_key.pop(uid, None)
    persist_user_to_db(uid)

def reset_count_if_needed(uid):
    with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if not doc:
                return
            info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
            user_keys[uid] = info
        if not info:
            return
        ws = info.get("window_start")
        if ws is None:
            info["count"] = 0
            info["window_start"] = now_ts()
            persist_user_to_db(uid)
            return
        elapsed = now_ts() - ws
        if elapsed >= WINDOW_SECONDS:
            info["count"] = 0
            info["window_start"] = now_ts()
            persist_user_to_db(uid)

def increment_count(uid):
    with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if not doc:
                return
            info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
            user_keys[uid] = info
        if not info:
            return
        info["count"] = info.get("count", 0) + 1
        if info.get("window_start") is None:
            info["window_start"] = now_ts()
        persist_user_to_db(uid)

def seconds_left_for_user(uid):
    with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if doc:
                info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
                user_keys[uid] = info
        if not info:
            return 0
        ws = info.get("window_start")
        if ws is None:
            return 0
        rem = WINDOW_SECONDS - (now_ts() - ws)
        return rem if rem > 0 else 0

def format_hms(secs):
    h = secs // 3600
    m = (secs % 3600) // 60
    s = secs % 60
    return f"{h}h {m}m {s}s"

def get_user_key_or_raise(uid):
    with lock:
        info = user_keys.get(uid)
        if not info and users_col is not None:
            doc = users_col.find_one({"uid": uid})
            if doc:
                info = {"key": doc.get("key"), "count": int(doc.get("count", 0)), "window_start": int(doc.get("window_start")) if doc.get("window_start") is not None else None}
                user_keys[uid] = info
        if not info or not info.get("key"):
            raise RuntimeError("API_KEY_MISSING")
        ws = info.get("window_start")
        if ws is None:
            info["window_start"] = now_ts()
            info["count"] = 0
            persist_user_to_db(uid)
            return info["key"]
        elapsed = now_ts() - ws
        if elapsed >= WINDOW_SECONDS:
            info["window_start"] = now_ts()
            info["count"] = 0
            persist_user_to_db(uid)
            return info["key"]
        if info.get("count", 0) >= DAILY_LIMIT:
            rem = WINDOW_SECONDS - elapsed
            raise RuntimeError(f"API_DAILY_LIMIT_REACHED|{int(rem)}")
        return info["key"]

def convert_to_wav(input_path: str) -> str:
    if not FFMPEG_BINARY:
        raise RuntimeError("FFmpeg binary not found.")
    output_path = os.path.join(DOWNLOADS_DIR, f"{os.path.basename(input_path).split('.')[0]}_converted.wav")
    command = [FFMPEG_BINARY, "-i", input_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", output_path, "-y"]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path

def gemini_api_call(endpoint, payload, key, headers=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    resp = requests.post(url, headers=headers or {"Content-Type": "application/json"}, json=payload, timeout=REQUEST_TIMEOUT_GEMINI)
    resp.raise_for_status()
    return resp.json()

async def upload_and_transcribe_gemini(file_path: str, uid: int) -> str:
    original_path, converted_path = file_path, None
    loop = asyncio.get_event_loop()
    def sync_transcribe():
        nonlocal file_path, converted_path
        if os.path.splitext(file_path)[1].lower() not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
            converted_path = convert_to_wav(file_path)
            file_path = converted_path
        file_size = os.path.getsize(file_path)
        mime_type = "audio/wav"
        key = get_user_key_or_raise(uid)
        uploaded_name = None
        try:
            upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
            headers = {
                "X-Goog-Upload-Protocol": "raw",
                "X-Goog-Upload-Command": "start, upload, finalize",
                "X-Goog-Upload-Header-Content-Length": str(file_size),
                "Content-Type": mime_type
            }
            with open(file_path, 'rb') as f:
                up_resp = requests.post(upload_url, headers=headers, data=f.read(), timeout=REQUEST_TIMEOUT_GEMINI).json()
            uploaded_name = up_resp.get("name", up_resp.get("file", {}).get("name"))
            uploaded_uri = up_resp.get("uri", up_resp.get("file", {}).get("uri"))
            if not uploaded_name:
                logging.error(f"Gemini Upload failed: {up_resp}")
                raise RuntimeError("Upload failed.")
            prompt = "Transcribe the audio in this file. Automatically detect the language and provide a clean transcription. Do not add intro phrases."
            payload = {"contents": [{"parts": [{"fileData": {"mimeType": mime_type, "fileUri": uploaded_uri}}, {"text": prompt}]}]}
            data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key)
            res_text = data["candidates"][0]["content"]["parts"][0]["text"]
            increment_count(uid)
            return res_text
        finally:
            if uploaded_name:
                try:
                    requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{uploaded_name}?key={key}", timeout=5)
                except:
                    pass
            if converted_path and os.path.exists(converted_path):
                os.remove(converted_path)
    return await loop.run_in_executor(None, sync_transcribe)

async def ask_gemini_async(text, instruction, uid):
    loop = asyncio.get_event_loop()
    def sync_ask():
        key = get_user_key_or_raise(uid)
        payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
        data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key)
        res_text = data["candidates"][0]["content"]["parts"][0]["text"]
        increment_count(uid)
        return res_text
    return await loop.run_in_executor(None, sync_ask)

def build_action_keyboard(text_len):
    btns = [[InlineKeyboardButton("⭐️ Get translating", callback_data="translate_menu|")]]
    if text_len > 1000:
        btns.append([InlineKeyboardButton("Summarize", callback_data="summarize|")])
    return InlineKeyboardMarkup(btns)

def build_lang_keyboard(origin):
    btns, row = [], []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}"))
        if i % 3 == 0:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    return InlineKeyboardMarkup(btns)

async def send_key_missing_alert(client: Client, chat_id):
    welcome_text = (
        "❌ API Key Required\n"
        "Please provide your Gemini API key to continue. The key should start with `AIzaSy...`\n"
        "Send your key now, or use /setkey YOUR_KEY\n"
        "You can get a key from [Google AI Studio](https://makersuite.google.com/app/apikey)."
    )
    try:
        await client.send_message(chat_id, welcome_text)
        user_awaiting_key[chat_id] = True
    except Exception as e:
        logging.error(f"Failed to send key missing alert: {e}")

app = Client("media_transcriber_gemini", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message(filters.command(["start", "help"]) & filters.private)
async def send_welcome(client: Client, message: Message):
    welcome_text = (
        "👋 **Salaam!**\n"
        "• Send me\n"
        "• **voice message**\n"
        "• **audio file**\n"
        "• **video**\n"
        "• to transcribe for free"
    )
    await message.reply_text(welcome_text)
    if not user_keys.get(message.from_user.id) or not user_keys.get(message.from_user.id).get("key"):
        await send_key_missing_alert(client, message.chat.id)

@app.on_message(filters.command("setkey") & filters.private)
async def setkey_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    uid = message.from_user.id
    if len(args) < 2:
        await message.reply_text("Usage: `/setkey YOUR_GEMINI_KEY`")
        return
    key = args[1].strip()
    if not is_gemini_key(key):
        user_awaiting_key[uid] = True
        await message.reply_text("❌ not Gemini key try again")
        return
    store_user_key(uid, key)
    await message.reply_text("☑️ Okay, your daily limit is 19 requests.\nNow send me the audio or video so I can transcribe")

@app.on_message(filters.private & filters.text)
async def text_handler(client: Client, message: Message):
    uid = message.from_user.id
    if user_awaiting_key.get(uid) and not message.text.startswith("/"):
        key = message.text.strip()
        if not is_gemini_key(key):
            user_awaiting_key[uid] = True
            await message.reply_text("❌ not Gemini key try again")
            return
        store_user_key(uid, key)
        await message.reply_text("☑️ Okay, your daily limit is 19 requests.\nNow send me the audio or video so I can transcribe")
        return
    if message.text.startswith("/getcount"):
        info = user_keys.get(uid)
        if not info:
            await send_key_missing_alert(client, message.chat.id)
            return
        reset_count_if_needed(uid)
        cnt = info.get('count', 0)
        rem = seconds_left_for_user(uid)
        if cnt >= DAILY_LIMIT:
            await message.reply_text(f"You have reached the daily limit of {DAILY_LIMIT}. Time remaining: {format_hms(rem)}.")
        else:
            await message.reply_text(f"Used: {cnt}. Remaining time in window: {format_hms(rem)}. Limit: {DAILY_LIMIT}.")
        return
    if message.text.startswith("/removekey"):
        if uid in user_keys:
            user_keys.pop(uid, None)
            if users_col is not None:
                users_col.delete_many({"uid": uid})
            await message.reply_text("Key removed from memory.")
        else:
            await message.reply_text("No key found.")
        return

@app.on_callback_query(filters.regex(r"^lang\|"))
async def lang_cb(client: Client, call: CallbackQuery):
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    _, code, lbl, origin = call.data.split("|")
    await process_text_action(client, call, call.message.message_id, f"Translate to {lbl}", f"Translate this text in to language {lbl}. No extra text ONLY return the translated text.")

@app.on_callback_query(filters.regex(r"^(translate_menu|summarize)\|"))
async def action_cb(client: Client, call: CallbackQuery):
    action, _ = call.data.split("|")
    if action == "translate_menu":
        await call.message.edit_reply_markup(reply_markup=build_lang_keyboard("trans"))
    else:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except:
            pass
        await process_text_action(client, call, call.message.message_id, "Summarize", "Summarize this in original language.")

async def process_text_action(client: Client, call: CallbackQuery, origin_msg_id, log_action, prompt_instr):
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    data = user_transcriptions.get(chat_id, {}).get(msg_id)
    if not data:
        await call.answer("Data not found (expired). Resend file.", show_alert=True)
        return
    text = data["text"]
    key = f"{chat_id}|{msg_id}|{log_action}"
    used = action_usage.get(key, 0)
    if "Summarize" in log_action and used >= 1:
        await call.answer("Already summarized!", show_alert=True)
        return
    await call.answer("Processing...")
    await client.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        res = await ask_gemini_async(text, prompt_instr, call.from_user.id)
        with lock:
            action_usage[key] = action_usage.get(key, 0) + 1
        persist_action_usage_to_db(key)
        await send_long_text(client, chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        msg = str(e)
        if "API_KEY_MISSING" in msg:
            await send_key_missing_alert(client, chat_id)
        elif "API_DAILY_LIMIT_REACHED" in msg:
            parts = msg.split("|")
            secs = int(parts[1]) if len(parts) > 1 else seconds_left_for_user(call.from_user.id)
            await client.send_message(chat_id, f"Daily limit reached. Time left: {format_hms(secs)}.")
        else:
            await client.send_message(chat_id, f"❌ Error: {e}")

@app.on_message(filters.private & (filters.voice | filters.audio | filters.video | filters.document))
async def handle_media(client: Client, message: Message):
    uid = message.from_user.id
    if not user_keys.get(uid) or not user_keys.get(uid).get("key"):
        await send_key_missing_alert(client, message.chat.id)
        return

    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        await message.reply_text(f"Just Send me a file less than {MAX_UPLOAD_MB}MB 😎")
        return

    await client.send_chat_action(message.chat.id, ChatAction.TYPING)
    file_path = None
    try:
        file_path = await client.download_media(message, file_name=os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{media.file_unique_id}"))
        try:
            text = await upload_and_transcribe_gemini(file_path, uid)
        except Exception as e:
            em = str(e)
            if "API_KEY_MISSING" in em:
                await send_key_missing_alert(client, message.chat.id)
                return
            if "API_DAILY_LIMIT_REACHED" in em:
                parts = em.split("|")
                secs = int(parts[1]) if len(parts) > 1 else seconds_left_for_user(uid)
                await message.reply_text(f"Daily limit reached. Time left: {format_hms(secs)}.")
                return
            raise
        if not text:
            raise ValueError("Empty response")
        sent = await send_long_text(client, message.chat.id, text, message.id, uid)
        if sent:
            sent_id = sent.id
            user_transcriptions.setdefault(message.chat.id, {})[sent_id] = {"text": text, "origin": message.id}
            try:
                await sent.edit_reply_markup(reply_markup=build_action_keyboard(len(text)))
            except Exception as e:
                logging.error(f"Failed to edit markup: {e}")
                pass
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

async def send_long_text(client: Client, chat_id, text, reply_id, uid, action="Transcript"):
    if len(text) > MAX_MESSAGE_CHUNK:
        fname = os.path.join(DOWNLOADS_DIR, f"{action}_{uid}.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(text)
        await client.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        sent = await client.send_document(chat_id, fname, caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
        os.remove(fname)
        return sent
    return await client.send_message(chat_id, text, reply_to_message_id=reply_id)

web_app = FastAPI()

@web_app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    total_users = len(user_keys)
    mongo_status = "Connected" if mongo_client else "Disconnected"
    sample_about = "This bot transcribes audio and video to text using Google's Gemini API. Upload a voice message or audio in Telegram, set your Gemini key with /setkey, and get clean transcriptions. Use Translate or Summarize actions on long transcriptions."
    html = f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width,initial-scale=1"/>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet"/>
      <title>SpeechBot - About</title>
      <style>
        body{{background: linear-gradient(135deg,#0f172a,#1e293b);color:#e6eef8}}
        .card{{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);}}
        .brand{{font-weight:700;letter-spacing:1px}}
        .chip{{display:inline-block;padding:.35rem .65rem;border-radius:999px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.04)}}
      </style>
    </head>
    <body>
      <div class="container py-5">
        <div class="row justify-content-center">
          <div class="col-md-10">
            <div class="d-flex align-items-center mb-4">
              <div class="me-3">
                <div style="width:72px;height:72px;border-radius:14px;background:linear-gradient(45deg,#06b6d4,#7c3aed);display:flex;align-items:center;justify-content:center;font-size:28px;color:white">SB</div>
              </div>
              <div>
                <div class="brand h3 mb-0">SpeechBot</div>
                <div class="text-muted">Transcribe · Translate · Summarize</div>
              </div>
            </div>
            <div class="card p-4 mb-4">
              <h5>Bit about</h5>
              <p style="font-size:15px;line-height:1.6">{sample_about}</p>
              <div class="d-flex gap-2">
                <div class="chip">Users: {total_users}</div>
                <div class="chip">MongoDB: {mongo_status}</div>
                <div class="chip">Daily limit per user: {DAILY_LIMIT}</div>
              </div>
            </div>
            <div class="row g-3">
              <div class="col-md-6">
                <div class="card p-3">
                  <h6>How to use</h6>
                  <ol>
                    <li>Open Telegram and start the bot.</li>
                    <li>Set your Gemini key with <code>/setkey YOUR_KEY</code>.</li>
                    <li>Send voice, audio, or video to transcribe.</li>
                  </ol>
                </div>
              </div>
              <div class="col-md-6">
                <div class="card p-3">
                  <h6>Quick links</h6>
                  <p><a href="https://t.me/{BOT_TOKEN.split(':')[0] if BOT_TOKEN else ''}" class="btn btn-sm btn-outline-light">Open Bot</a> <a href="#" class="btn btn-sm btn-outline-light">Docs</a></p>
                </div>
              </div>
            </div>
            <footer class="mt-4 text-muted small">Hosted on your server · Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}</footer>
          </div>
        </div>
      </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html, status_code=200)

def start_web():
    uvicorn.run(web_app, host=WEB_HOST, port=WEB_PORT, log_level="info")

if __name__ == "__main__":
    logging.info("Starting web interface thread")
    t = threading.Thread(target=start_web, daemon=True)
    t.start()
    logging.info("Starting Pyrogram client...")
    app.run()
