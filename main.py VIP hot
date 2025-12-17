import os
import threading
import time
import subprocess
import asyncio
import logging
import requests
import pymongo
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from google import genai
from google.genai.errors import APIError
from flask import Flask, render_template_string, jsonify

FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "/usr/bin/ffmpeg")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
PORT = int(os.environ.get("PORT", "8080"))
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))
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
user_model_usage = {}
MAX_USAGE_COUNT = 18
PRIMARY_MODEL = GEMINI_MODEL
FALLBACK_MODEL = GEMINI_FALLBACK_MODEL

app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
client_mongo = None
db = None
users_col = None

try:
    client_mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client_mongo.admin.command("ping")
    except:
        client_mongo = None
    if client_mongo:
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
        try:
            cursor = users_col.find({}, {"user_id": 1, "gemini_key": 1})
            for doc in cursor:
                try:
                    user_gemini_keys[int(doc["user_id"])] = doc.get("gemini_key")
                except:
                    pass
        except:
            pass
except Exception as e:
    logging.warning("MongoDB connection failed: %s", e)

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
    return user_mode.get(uid, "Split messages")

def convert_to_optimized_audio(input_path: str) -> str:
    if not FFMPEG_BINARY:
        raise RuntimeError("FFmpeg binary not found.")
    base = os.path.splitext(os.path.basename(input_path))[0]
    output_path = os.path.join(DOWNLOADS_DIR, f"{base}_opt.mp3")
    command = [
        FFMPEG_BINARY,
        "-i", input_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "32k",
        "-y",
        output_path
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path

def upload_and_transcribe_gemini(file_path: str, key: str, uid: int) -> str:
    original_path = file_path
    converted_path = None
    uploaded_file = None
    if os.path.splitext(file_path)[1].lower() not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
        try:
            converted_path = convert_to_optimized_audio(file_path)
            file_path = converted_path
        except Exception as e:
            logging.warning("Conversion failed, using original: %s", e)
    else:
        ext = os.path.splitext(file_path)[1].lower()
        if ext != ".mp3":
            try:
                converted_path = convert_to_optimized_audio(file_path)
                file_path = converted_path
            except Exception as e:
                logging.warning("Conversion failed, using original: %s", e)
    try:
        client = genai.Client(api_key=key)
        uploaded_file = client.files.upload(file=file_path)
        prompt = "Transcribe this audio and provide a clean transcription. Do not add intro phrases."
        current_model = get_current_model(uid)
        response = client.models.generate_content(
            model=current_model,
            contents=[prompt, uploaded_file]
        )
        return response.text
    except APIError as e:
        raise RuntimeError(f"Gemini API Error: {e}")
    except Exception as e:
        raise RuntimeError(f"Error during transcription: {e}")
    finally:
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
            except:
                pass
        if converted_path and os.path.exists(converted_path):
            try:
                os.remove(converted_path)
            except:
                pass
        if original_path and os.path.exists(original_path):
            try:
                os.remove(original_path)
            except:
                pass

def ask_gemini(text, instruction, key, uid):
    try:
        client = genai.Client(api_key=key)
        prompt = f"{instruction}\n\n{text}"
        current_model = get_current_model(uid)
        response = client.models.generate_content(
            model=current_model,
            contents=[prompt]
        )
        return response.text
    except APIError as e:
        raise RuntimeError(f"Gemini API Error: {e}")
    except Exception as e:
        raise RuntimeError(f"Error during processing: {e}")

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
    if not REQUIRED_CHANNEL:
        return True
    try:
        user_id = message.from_user.id
        member = await client.get_chat_member(REQUIRED_CHANNEL, user_id)
        if member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]:
            return True
    except:
        pass
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
    if not await ensure_joined(client, message):
        return
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
    if not await ensure_joined(client, call.message):
        return
    mode = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode
    try:
        await call.edit_message_text(f"you choosed: {mode}")
    except:
        pass
    await call.answer(f"Mode set to: {mode} ☑️")

@app.on_callback_query(filters.regex(r"^summarize_menu\|"))
async def summarize_menu_cb(client, call):
    try:
        await call.edit_message_reply_markup(reply_markup=build_summarize_keyboard(call.message.id))
    except:
        try:
            await call.answer("Opening summarize options...")
        except:
            pass

@app.on_callback_query(filters.regex(r"^summopt\|"))
async def summopt_cb(client, call):
    try:
        _, style, origin = call.data.split("|")
    except:
        await call.answer("Invalid option", show_alert=True)
        return
    try:
        await call.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    prompt = ""
    if style == "Short":
        prompt = "Summarize this text in the original language in 1-2 concise sentences. No extra text — return only the summary."
    elif style == "Detailed":
        prompt = "Summarize this text in the original language in a detailed paragraph preserving key points. No extra text — return only the summary."
    else:
        prompt = "Summarize this text in the original language as a bulleted list of main points. No extra text — return only the summary."
    await process_text_action(client, call, origin, f"Summarize ({style})", prompt)

async def process_text_action(client, call, origin_msg_id, log_action, prompt_instr):
    if not await ensure_joined(client, call.message):
        return
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
    if not await ensure_joined(client, message):
        return
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
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
        if not text:
            raise ValueError("Empty response")
        sent = await send_long_text(client, message.chat.id, text, message.id, message.from_user.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.id] = {"text": text, "origin": message.id}
            if len(text) > 1000:
                await client.edit_message_reply_markup(message.chat.id, sent.id, reply_markup=build_action_keyboard(len(text)))
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}", quote=True)
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except:
            pass

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
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            sent = await client.send_document(chat_id, fname, caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            try:
                os.remove(fname)
            except:
                pass
            return sent
    return await client.send_message(chat_id, text, reply_to_message_id=reply_id)

flask_app = Flask(__name__)
start_time = time.time()

@flask_app.route("/")
def index():
    uptime = int(time.time() - start_time)
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Bot Status</title>
        <style>
          body {font-family: Arial, Helvetica, sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; background:#f6f7fb}
          .card {background:#fff;padding:24px;border-radius:12px;box-shadow:0 6px 20px rgba(0,0,0,0.08);text-align:center}
          .dot {height:12px;width:12px;background:#4caf50;border-radius:50%;display:inline-block;margin-right:8px}
          .uptime {color:#666;margin-top:8px;font-size:14px}
        </style>
      </head>
      <body>
        <div class="card">
          <div><span class="dot"></span><strong>I'm online</strong></div>
          <div class="uptime">Uptime: {{uptime}} seconds</div>
        </div>
      </body>
    </html>
    """
    return render_template_string(html, uptime=uptime)

@flask_app.route("/status")
def status():
    return jsonify({"status":"online","uptime_seconds": int(time.time() - start_time)})

if __name__ == "__main__":
    threading.Thread(target=flask_app.run, kwargs={"host":"0.0.0.0","port":PORT,"debug":False,"use_reloader":False}, daemon=True).start()
    app.run()
