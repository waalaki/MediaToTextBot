import os
import threading
import time
import subprocess
import asyncio
import logging
import requests
import pymongo
from flask import Flask, render_template_string, request
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "/usr/bin/ffmpeg")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
PORT = int(os.environ.get("PORT", "8080"))
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "200"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "5240873494"))
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_APPNAME = os.environ.get("DB_APPNAME", "SpeechBot")
MONGO_URI = os.environ.get("MONGO_URI") or f"mongodb+srv://{DB_USER}:{DB_PASSWORD}@cluster0.n4hdlxk.mongodb.net/{DB_APPNAME}?retryWrites=true&w=majority&appName={DB_APPNAME}"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
user_gemini_keys = {}
user_mode = {}
user_transcriptions = {}
action_usage = {}
app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
flask_app = Flask(__name__)
client_mongo = None
db = None
users_col = None
try:
    client_mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    if DB_APPNAME:
        db = client_mongo[DB_APPNAME]
    else:
        try:
            db = client_mongo.get_default_database()
        except:
            db = None
    if db is not None:
        users_col = db.get_collection("users")
        try:
            users_col.create_index("user_id", unique=True)
        except:
            pass
        for doc in users_col.find({}, {"user_id": 1, "gemini_key": 1}):
            try:
                user_gemini_keys[int(doc["user_id"])] = doc.get("gemini_key")
            except:
                pass
except Exception as e:
    logging.warning("MongoDB connection failed: %s", e)
    
user_model_usage = {}
MAX_USAGE_COUNT = 18
PRIMARY_MODEL = GEMINI_MODEL
FALLBACK_MODEL = GEMINI_FALLBACK_MODEL

def get_current_model(uid):
    current_usage = user_model_usage.get(uid, {"primary_count": 0, "fallback_count": 0, "current_model": PRIMARY_MODEL})
    
    if current_usage["current_model"] == PRIMARY_MODEL:
        if current_usage["primary_count"] < MAX_USAGE_COUNT:
            current_usage["primary_count"] += 1
            user_model_usage[uid] = current_usage
            return PRIMARY_MODEL
        else:
            current_usage["current_model"] = FALLBACK_MODEL
            current_usage["primary_count"] = 0
            current_usage["fallback_count"] = 1
            user_model_usage[uid] = current_usage
            return FALLBACK_MODEL
    
    elif current_usage["current_model"] == FALLBACK_MODEL:
        if current_usage["fallback_count"] < MAX_USAGE_COUNT:
            current_usage["fallback_count"] += 1
            user_model_usage[uid] = current_usage
            return FALLBACK_MODEL
        else:
            current_usage["current_model"] = PRIMARY_MODEL
            current_usage["fallback_count"] = 0
            current_usage["primary_count"] = 1
            user_model_usage[uid] = current_usage
            return PRIMARY_MODEL
    
    user_model_usage[uid] = {"primary_count": 1, "fallback_count": 0, "current_model": PRIMARY_MODEL}
    return PRIMARY_MODEL

def set_user_key_db(uid, key):
    try:
        if users_col is not None:
            users_col.update_one({"user_id": uid}, {"$set": {"gemini_key": key, "updated_at": time.time()}}, upsert=True)
        user_gemini_keys[uid] = key
    except Exception as e:
        logging.warning("Failed to set key in DB: %s", e)
        user_gemini_keys[uid] = key
def get_user_key_db(uid):
    try:
        if uid in user_gemini_keys:
            return user_gemini_keys[uid]
        if users_col is not None:
            doc = users_col.find_one({"user_id": uid})
            if doc:
                key = doc.get("gemini_key")
                user_gemini_keys[uid] = key
                return key
    except Exception as e:
        logging.warning("Failed to get key from DB: %s", e)
    return user_gemini_keys.get(uid)
def get_user_mode(uid):
    return user_mode.get(uid, "📄 Text File")
def convert_to_wav(input_path: str) -> str:
    if not FFMPEG_BINARY: raise RuntimeError("FFmpeg binary not found.")
    output_path = os.path.join(DOWNLOADS_DIR, f"{os.path.basename(input_path).split('.')[0]}_converted.wav")
    command = [FFMPEG_BINARY, "-i", input_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", output_path, "-y"]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path
def gemini_api_call(endpoint, payload, key, model_name, headers=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_GEMINI)
    resp.raise_for_status()
    return resp.json()

def upload_and_transcribe_gemini(file_path: str, key: str, uid: int) -> str:
    original_path, converted_path = file_path, None
    if os.path.splitext(file_path)[1].lower() not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
        converted_path = convert_to_wav(file_path)
        file_path = converted_path
    file_size = os.path.getsize(file_path)
    mime_type = "audio/wav"
    uploaded_name = None
    try:
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
        headers = {
            "X-Goog-Upload-Protocol": "raw", "X-Goog-Upload-Command": "start, upload, finalize",
            "X-Goog-Upload-Header-Content-Length": str(file_size), "Content-Type": mime_type
        }
        with open(file_path, "rb") as f:
            up_resp = requests.post(upload_url, headers=headers, data=f.read(), timeout=REQUEST_TIMEOUT_GEMINI).json()
        uploaded_name = up_resp.get("name", up_resp.get("file", {}).get("name"))
        uploaded_uri = up_resp.get("uri", up_resp.get("file", {}).get("uri"))
        if not uploaded_name: raise RuntimeError("Upload failed.")
        prompt = "Transcribe this audio and provide a clean transcription. Do not add intro phrases."
        payload = {"contents": [{"parts": [{"fileData": {"mimeType": mime_type, "fileUri": uploaded_uri}}, {"text": prompt}]}]}
        
        current_model = get_current_model(uid)
        
        data = gemini_api_call(f"models/{current_model}:generateContent", payload, key, current_model, headers={"Content-Type": "application/json"})
        return data["candidates"][0]["content"]["parts"][0]["text"]
        
    finally:
        if uploaded_name:
            try: requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{uploaded_name}?key={key}", timeout=5)
            except: pass
        if converted_path and os.path.exists(converted_path):
            os.remove(converted_path)

def ask_gemini(text, instruction, key, uid):
    payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
    current_model = get_current_model(uid)
    
    data = gemini_api_call(f"models/{current_model}:generateContent", payload, key, current_model, headers={"Content-Type": "application/json"})
    return data["candidates"][0]["content"]["parts"][0]["text"]

def build_action_keyboard(text_len):
    btns = []
    if text_len > 1000:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
    return InlineKeyboardMarkup(btns)
def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)
async def ensure_joined(client, message):
    if not REQUIRED_CHANNEL: return True
    try:
        user_id = message.from_user.id
        member = await client.get_chat_member(REQUIRED_CHANNEL, user_id)
        if member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return True
    except: pass
    clean = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean}")]])
    await message.reply_text("First, join my channel and come back 👍", reply_markup=kb, quote=True)
    return False
@app.on_message(filters.command(["start", "help"]))
async def send_welcome(client, message):
    if await ensure_joined(client, message):
        welcome_text = "👋 Salaam!\n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free"
        await message.reply_text(welcome_text, quote=True)
@app.on_message(filters.command("mode"))
async def choose_mode(client, message):
    if await ensure_joined(client, message):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
            [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
        ])
        await message.reply_text("How do I send you long transcripts?:", reply_markup=kb, quote=True)
@app.on_message(filters.regex(r"^AIz"))
async def set_key_plain(client, message):
    if not await ensure_joined(client, message): return
    token = message.text.strip().split()[0]
    if not token.startswith("AIz"):
        await message.reply_text("Invalid key 🙅🏻", quote=True)
        return
    prev = get_user_key_db(message.from_user.id)
    set_user_key_db(message.from_user.id, token)
    if prev:
        await message.reply_text("API key updated.", quote=True)
    else:
        await message.reply_text("Okay send me audio or video 👍", quote=True)
        try:
            uname = message.from_user.username or "N/A"
            uid = message.from_user.id
            fname = message.from_user.first_name or ""
            lang = message.from_user.language_code or ""
            info = f"New user provided Gemini key\nUsername: @{uname}\nId: {uid}\nFirst: {fname}\nLang: {lang}"
            await client.send_message(ADMIN_ID, info)
        except Exception as e:
            logging.warning("Failed to notify admin: %s", e)
@app.on_callback_query(filters.regex(r"^mode\|"))
async def mode_cb(client, call):
    if not await ensure_joined(client, call.message): return
    mode = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode
    try:
        await call.edit_message_text(f"you choosed: {mode}")
    except: pass
    await call.answer(f"Mode set to: {mode} ☑️")
@app.on_callback_query(filters.regex(r"^summarize_menu\|"))
async def summarize_menu_cb(client, call):
    try:
        await call.edit_message_reply_markup(reply_markup=build_summarize_keyboard(call.message.id))
    except:
        try: await call.answer("Opening summarize options...")
        except: pass
@app.on_callback_query(filters.regex(r"^summopt\|"))
async def summopt_cb(client, call):
    try:
        _, style, origin = call.data.split("|")
    except:
        await call.answer("Invalid option", show_alert=True)
        return
    try:
        await call.edit_message_reply_markup(reply_markup=None)
    except: pass
    prompt = ""
    if style == "Short":
        prompt = "Summarize this text in the original language in 1-2 concise sentences. No extra text — return only the summary."
    elif style == "Detailed":
        prompt = "Summarize this text in the original language in a detailed paragraph preserving key points. No extra text — return only the summary."
    else:
        prompt = "Summarize this text in the original language as a bulleted list of main points. No extra text — return only the summary."
    await process_text_action(client, call, origin, f"Summarize ({style})", prompt)
async def process_text_action(client, call, origin_msg_id, log_action, prompt_instr):
    if not await ensure_joined(client, call.message): return
    chat_id, msg_id = call.message.chat.id, call.message.id
    try:
        origin_id = int(origin_msg_id)
    except:
        origin_id = call.message.id
    data = user_transcriptions.get(chat_id, {}).get(origin_id)
    if not data:
        if call.message.reply_to_message:
             data = user_transcriptions.get(chat_id, {}).get(call.message.reply_to_message.id)
    if not data:
        await call.answer("Data not found (expired). Resend file.", show_alert=True)
        return
    text = data["text"]
    key = f"{chat_id}|{msg_id}|{log_action}"
    user_key = get_user_key_db(call.from_user.id)
    if not user_key:
        await call.answer("Gemini key not set 🙅🏻‍♂️", show_alert=True)
        return
    await call.answer("Processing...")
    await client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    try:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, ask_gemini, text, prompt_instr, user_key, call.from_user.id)
        if "Summarize" not in log_action:
            action_usage[key] = action_usage.get(key, 0) + 1
        await send_long_text(client, chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        await client.send_message(chat_id, f"❌ Error: {e}")
@app.on_message(filters.voice | filters.audio | filters.video | filters.document)
async def handle_media(client, message):
    if not await ensure_joined(client, message): return
    media = message.voice or message.audio or message.video or message.document
    if not media: return
    if getattr(media, "file_size", 0) > MAX_UPLOAD_SIZE:
        await message.reply_text(f"Just Send me a file less than {MAX_UPLOAD_MB}MB 😎", quote=True)
        return
    user_key = get_user_key_db(message.from_user.id)
    if not user_key:
        await message.reply_text("first send me Gemini key 🤓", quote=True)
        try:
            if REQUIRED_CHANNEL:
                me = await client.get_me()
                try:
                    bot_member = await client.get_chat_member(REQUIRED_CHANNEL, me.id)
                    if bot_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
                        chat_info = await client.get_chat(REQUIRED_CHANNEL)
                        pinned = chat_info.pinned_message
                        if pinned:
                            try:
                                await pinned.forward(message.chat.id)
                            except Exception as e:
                                logging.warning("Failed to forward pinned message: %s", e)
                except Exception as e:
                    logging.warning("Failed to check bot admin status or forward pinned message: %s", e)
        except Exception as e:
            logging.warning("Unexpected error: %s", e)
        return
    await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
    file_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{media.file_unique_id}")
    try:
        d_path = await client.download_media(message, file_name=file_path)
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, upload_and_transcribe_gemini, d_path, user_key, message.from_user.id)
        if not text: raise ValueError("Empty response")
        sent = await send_long_text(client, message.chat.id, text, message.id, message.from_user.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.id] = {"text": text, "origin": message.id}
            if len(text) > 1000:
                await client.edit_message_reply_markup(message.chat.id, sent.id, reply_markup=build_action_keyboard(len(text)))
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}", quote=True)
    finally:
        if os.path.exists(file_path): os.remove(file_path)
async def send_long_text(client, chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid)
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                sent = await client.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(fname, "w", encoding="utf-8") as f: f.write(text)
            sent = await client.send_document(chat_id, fname, caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            os.remove(fname)
            return sent
    return await client.send_message(chat_id, text, reply_to_message_id=reply_id)
html_template = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SpeechBot — Voice → Text</title>
<style>
:root{
  --bg1:#0f172a;
  --bg2:#0b1220;
  --card:#0b1228;
  --accent:#7c3aed;
  --muted:#94a3b8;
  --glass: rgba(255,255,255,0.04);
}
*{box-sizing:border-box;margin:0;padding:0;font-family:Inter,ui-sans-serif,system-ui,Segoe UI,Roboto,"Helvetica Neue",Arial}
body{min-height:100vh;background:linear-gradient(135deg,var(--bg1) 0%,var(--bg2) 100%);color:#e6eef8;padding:36px;display:flex;align-items:center;justify-content:center}
.container{width:100%;max-width:1100px}
.header{display:flex;align-items:center;gap:20px;margin-bottom:28px}
.logo{width:76px;height:76px;border-radius:14px;background:linear-gradient(135deg,var(--accent),#06b6d4);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:28px;box-shadow:0 8px 30px rgba(12,12,20,0.6)}
.title{font-size:22px;font-weight:700}
.subtitle{color:var(--muted);font-size:14px;margin-top:6px}
.grid{display:grid;grid-template-columns:1fr 380px;gap:22px}
.card{background:linear-gradient(180deg,rgba(255,255,255,0.02),rgba(255,255,255,0.01));border-radius:12px;padding:22px;box-shadow:0 6px 24px rgba(2,6,23,0.6);backdrop-filter:blur(6px)}
.hero h2{font-size:18px;margin-bottom:8px}
.badge{display:inline-flex;padding:8px 12px;border-radius:999px;background:var(--glass);color:var(--muted);font-weight:600;font-size:13px;margin-bottom:12px}
.features{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:12px}
.feature{background:rgba(255,255,255,0.02);padding:12px;border-radius:10px}
.steps{display:flex;flex-direction:column;gap:10px;margin-top:12px}
.step{display:flex;gap:12px;align-items:flex-start}
.step .num{width:36px;height:36px;border-radius:8px;background:rgba(255,255,255,0.03);display:flex;align-items:center;justify-content:center;font-weight:700}
.footer{margin-top:18px;color:var(--muted);font-size:13px}
.linkbtn{display:inline-flex;align-items:center;gap:10px;padding:10px 14px;border-radius:10px;background:linear-gradient(90deg,var(--accent),#06b6d4);color:#051025;font-weight:700;text-decoration:none}
.panel{display:flex;flex-direction:column;gap:12px}
.meta{display:flex;gap:10px;align-items:center}
.meta .info{font-size:13px;color:var(--muted)}
.code{background:#071127;padding:12px;border-radius:8px;color:#a7b9d9;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,monospace;font-size:13px;overflow:auto;max-height:200px}
.note{font-size:13px;color:var(--muted)}
@media (max-width:980px){.grid{grid-template-columns:1fr}.header{flex-direction:row;gap:12px}.logo{width:64px;height:64px}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">SB</div>
    <div>
      <div class="title">SpeechBot — Voice to Text with Gemini</div>
      <div class="subtitle">Transcribe voice, audio and video from Telegram. Summarize and download clean transcripts.</div>
    </div>
  </div>
  <div class="grid">
    <div class="card hero">
      <div class="badge">Fast · Private · Multilingual</div>
      <h2>What this bot does</h2>
      <p class="note">Send voice messages, audio files or videos in Telegram to get a clean transcription. Optionally provide a Gemini API key (starts with <strong>AIz</strong>) to enable on-demand language understanding and summarization powered by Gemini.</p>
      <div class="features">
        <div class="feature">
          <strong>High quality transcripts</strong>
          <div class="note">Automatic audio conversion and transcription with fallback models for resilience.</div>
        </div>
        <div class="feature">
          <strong>Summarize</strong>
          <div class="note">Produce short, detailed or bulleted summaries.</div>
        </div>
        <div class="feature">
          <strong>File output</strong>
          <div class="note">Long transcripts can be sent as split messages or a downloadable text file.</div>
        </div>
        <div class="feature">
          <strong>Limits & Safety</strong>
          <div class="note">Max upload size configured by the bot owner. Admin notifications for new API key registrations.</div>
        </div>
      </div>
      <div class="steps">
        <div class="step"><div class="num">1</div><div><strong>Start the bot</strong><div class="note">Open Telegram and press /start with the bot.</div></div></div>
        <div class="step"><div class="num">2</div><div><strong>Provide a Gemini key (optional)</strong><div class="note">Send a message that begins with your Gemini API key (AIz... ). This unlocks summarization features.</div></div></div>
        <div class="step"><div class="num">3</div><div><strong>Send audio</strong><div class="note">Send voice, audio or video. The bot will transcribe and reply with the text or a text file.</div></div></div>
        <div class="step"><div class="num">4</div><div><strong>Use actions</strong><div class="note">After transcription use the inline buttons to summarize results.</div></div></div>
      </div>
      <div class="footer">If the bot requires you to join a channel before use, the bot will prompt you and provide a link inside Telegram.</div>
    </div>
    <div class="card panel">
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div><strong>Bot status</strong><div class="note">Current operational metadata for the running bot instance.</div></div>
          <div id="statusBadge" class="badge">Loading</div>
        </div>
        <div class="meta" style="margin-top:12px">
          <div class="info" id="botInfo">Fetching bot info…</div>
        </div>
      </div>
      <div>
        <strong>Quick links</strong>
        <div style="display:flex;flex-direction:column;gap:8px;margin-top:8px">
          <a id="openBot" class="linkbtn" target="_blank" rel="noopener noreferrer">Open in Telegram</a>
          <a id="howto" class="linkbtn" href="#howto">Usage tips</a>
        </div>
      </div>
      <div>
        <strong>Configuration</strong>
        <div class="note" style="margin-top:8px">Environment-driven configuration visible to the owner.</div>
        <div class="code" id="envbox"></div>
      </div>
      <div>
        <strong>Example commands</strong>
        <div class="code" style="margin-top:8px">/start
/mode
AIzYOUR_GEMINI_KEY_HERE
Send voice or audio file</div>
      </div>
    </div>
  </div>
  <div style="margin-top:20px" id="howto">
    <div class="card">
      <h3>Notes for operators</h3>
      <p class="note">This web interface is served by the bot process and displays runtime information. The bot uses a MongoDB collection to store per-user Gemini keys and basic telemetry for action usage. Change environment variables to adjust limits and models.</p>
    </div>
  </div>
</div>
<script>
async function fetchStatus(){
  try{
    const resp = await fetch("/_status");
    const j = await resp.json();
    document.getElementById("statusBadge").textContent = j.status || "Running";
    const botInfo = j.bot_name ? `${j.bot_name} (${j.bot_username}) • id ${j.bot_id}` : j.service || "SpeechBot";
    document.getElementById("botInfo").textContent = botInfo;
    const openBot = document.getElementById("openBot");
    if(j.bot_username){
      openBot.href = `https://t.me/${j.bot_username.replace("@","")}`;
      openBot.textContent = "Open @" + j.bot_username.replace("@","");
    } else {
      openBot.style.display = "none";
    }
    const env = {
      PORT: "{{PORT}}",
      MAX_UPLOAD_MB: "{{MAX_UPLOAD_MB}}",
      GEMINI_MODEL: "{{GEMINI_MODEL}}",
      REQUIRED_CHANNEL: "{{REQUIRED_CHANNEL}}"
    };
    document.getElementById("envbox").textContent = JSON.stringify(env, null, 2);
  }catch(e){
    document.getElementById("statusBadge").textContent = "Offline";
    document.getElementById("botInfo").textContent = "Bot info unavailable";
  }
}
fetchStatus();
setInterval(fetchStatus, 15000);
</script>
</body>
</html>
"""
@flask_app.route("/")
def index():
    try:
        me = app.get_me()
        bot_username = f"@{me.username}" if getattr(me, "username", None) else ""
        bot_name = me.first_name if getattr(me, "first_name", None) else ""
        bot_id = me.id if getattr(me, "id", None) else ""
    except:
        bot_username = ""
        bot_name = ""
        bot_id = ""
    rendered = html_template.replace("{{PORT}}", str(PORT)).replace("{{MAX_UPLOAD_MB}}", str(MAX_UPLOAD_MB)).replace("{{GEMINI_MODEL}}", GEMINI_MODEL).replace("{{REQUIRED_CHANNEL}}", REQUIRED_CHANNEL or "")
    return render_template_string(rendered, bot_username=bot_username, bot_name=bot_name, bot_id=bot_id)
@flask_app.route("/_status")
def status():
    try:
        me = app.get_me()
        info = {
            "status": "Online",
            "bot_name": me.first_name,
            "bot_username": f"@{me.username}" if getattr(me, "username", None) else "",
            "bot_id": me.id,
            "service": "SpeechBot Pyrogram"
        }
    except:
        info = {"status": "Running", "service": "SpeechBot"}
    return info
def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)
if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    app.run()
