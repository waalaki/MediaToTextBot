import os
import threading
import json
import requests
import logging
import time
import base64
import mimetypes
import pymongo
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
GEMINI_KEYS = os.environ.get("GEMINI_KEYS", GEMINI_KEY)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite")
MAX_MODEL_USAGE = int(os.environ.get("MAX_MODEL_USAGE", "18"))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_APPNAME = os.environ.get("DB_APPNAME", "SpeechBot")
MONGO_URI = os.environ.get("MONGO_URI") or f"mongodb+srv://{DB_USER}:{DB_PASSWORD}@cluster0.n4hdlxk.mongodb.net/{DB_APPNAME}?retryWrites=true&w=majority&appName={DB_APPNAME}"
FREE_USES = int(os.environ.get("FREE_USES", "3"))

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KeyRotator:
    def __init__(self, keys):
        self.keys = [k.strip() for k in keys.split(",") if k.strip()] if isinstance(keys, str) else list(keys or [])
        self.pos = 0
        self.lock = threading.Lock()
    def get_key(self):
        with self.lock:
            if not self.keys:
                return None
            key = self.keys[self.pos]
            self.pos = (self.pos + 1) % len(self.keys)
            return key
    def mark_success(self, key):
        with self.lock:
            try:
                i = self.keys.index(key)
                self.pos = (i + 1) % len(self.keys)
            except ValueError:
                pass
    def mark_failure(self, key):
        self.mark_success(key)

gemini_rotator = KeyRotator(GEMINI_KEYS)

class ModelSwitcher:
    def __init__(self, primary, fallback, max_count=18):
        self.primary = primary
        self.fallback = fallback
        self.max_count = max_count
        self.usage = {}
        self.lock = threading.Lock()
    def get_model(self, uid):
        with self.lock:
            state = self.usage.get(uid, {"primary_count": 0, "fallback_count": 0, "current": self.primary})
            if state["current"] == self.primary:
                if state["primary_count"] < self.max_count:
                    state["primary_count"] += 1
                    self.usage[uid] = state
                    return self.primary
                else:
                    state["current"] = self.fallback
                    state["primary_count"] = 0
                    state["fallback_count"] = 1
                    self.usage[uid] = state
                    return self.fallback
            else:
                if state["fallback_count"] < self.max_count:
                    state["fallback_count"] += 1
                    self.usage[uid] = state
                    return self.fallback
                else:
                    state["current"] = self.primary
                    state["fallback_count"] = 0
                    state["primary_count"] = 1
                    self.usage[uid] = state
                    return self.primary

model_switcher = ModelSwitcher(GEMINI_MODEL, GEMINI_FALLBACK_MODEL, MAX_MODEL_USAGE)

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

user_mode = {}
user_transcriptions = {}
action_usage = {}
user_selected_lang = {}
pending_files = {}
user_gemini_keys = {}

client_mongo = None
db = None
users_col = None

try:
    if MONGO_URI:
        client_mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        client_mongo.admin.command("ping")
        db = client_mongo.get_default_database() if client_mongo else None
        if db is None:
            db = client_mongo.get_database("SpeechBot")
        users_col = db.get_collection("users")
        users_col.create_index("user_id", unique=True)
        for doc in users_col.find({}, {"user_id": 1, "gemini_key": 1, "free_uses": 1, "mode": 1, "selected_lang": 1}):
            uid = int(doc.get("user_id"))
            user_gemini_keys[uid] = {"key": doc.get("gemini_key"), "free_uses": int(doc.get("free_uses", 0))}
            if doc.get("mode"):
                user_mode[uid] = doc.get("mode")
            if doc.get("selected_lang"):
                user_selected_lang[uid] = doc.get("selected_lang")
except Exception as e:
    logging.warning("MongoDB connection failed: %s", e)

def set_user_key_db(uid, key):
    try:
        if users_col is not None:
            users_col.update_one({"user_id": uid}, {"$set": {"gemini_key": key, "updated_at": time.time()}}, upsert=True)
        entry = user_gemini_keys.get(uid, {})
        entry["key"] = key
        user_gemini_keys[uid] = entry
    except Exception as e:
        logging.warning("Failed to set key in DB: %s", e)
        entry = user_gemini_keys.get(uid, {})
        entry["key"] = key
        user_gemini_keys[uid] = entry

def get_user_key_db(uid):
    entry = user_gemini_keys.get(uid)
    if entry and entry.get("key"):
        return entry.get("key")
    try:
        if users_col is not None:
            doc = users_col.find_one({"user_id": uid})
            if doc:
                key = doc.get("gemini_key")
                free_uses = int(doc.get("free_uses", 0))
                user_gemini_keys[uid] = {"key": key, "free_uses": free_uses}
                return key
    except Exception as e:
        logging.warning("Failed to get key from DB: %s", e)
    return None

def get_free_uses_remaining(uid):
    entry = user_gemini_keys.get(uid)
    if entry:
        used = int(entry.get("free_uses", 0))
        return max(0, FREE_USES - used)
    try:
        if users_col is not None:
            doc = users_col.find_one({"user_id": uid})
            if doc:
                used = int(doc.get("free_uses", 0))
                user_gemini_keys[uid] = {"key": doc.get("gemini_key"), "free_uses": used}
                return max(0, FREE_USES - used)
    except Exception as e:
        logging.warning("Failed to read free uses: %s", e)
    return FREE_USES

def increment_free_use(uid):
    entry = user_gemini_keys.get(uid, {"key": None, "free_uses": 0})
    entry["free_uses"] = int(entry.get("free_uses", 0)) + 1
    user_gemini_keys[uid] = entry
    try:
        if users_col is not None:
            users_col.update_one({"user_id": uid}, {"$set": {"free_uses": entry["free_uses"], "updated_at": time.time()}}, upsert=True)
    except Exception as e:
        logging.warning("Failed to increment free uses: %s", e)

def set_user_mode_db(uid, mode):
    user_mode[uid] = mode
    try:
        if users_col is not None:
            users_col.update_one({"user_id": uid}, {"$set": {"mode": mode, "updated_at": time.time()}}, upsert=True)
    except Exception as e:
        logging.warning("Failed to set mode in DB: %s", e)

def set_user_selected_lang_db(uid, code):
    user_selected_lang[uid] = code
    try:
        if users_col is not None:
            users_col.update_one({"user_id": uid}, {"$set": {"selected_lang": code, "updated_at": time.time()}}, upsert=True)
    except Exception as e:
        logging.warning("Failed to set selected_lang in DB: %s", e)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
flask_app = Flask(__name__)

def get_user_mode(uid):
    m = user_mode.get(uid)
    if m:
        return m
    try:
        if users_col is not None:
            doc = users_col.find_one({"user_id": uid}, {"mode": 1})
            if doc and doc.get("mode"):
                user_mode[uid] = doc.get("mode")
                return doc.get("mode")
    except Exception as e:
        logging.warning("Failed to read mode from DB: %s", e)
    return "📄 Text File"

def gemini_api_call(endpoint, payload, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def execute_gemini_action(action_callback):
    last_exc = None
    total = len(gemini_rotator.keys) or 1
    for _ in range(total + 1):
        key = gemini_rotator.get_key()
        if not key:
            raise RuntimeError("No Gemini keys available")
        try:
            result = action_callback(key)
            gemini_rotator.mark_success(key)
            return result
        except Exception as e:
            last_exc = e
            logging.warning(f"Gemini error with key {str(key)[:4]}: {e}")
            gemini_rotator.mark_failure(key)
    raise RuntimeError(f"Gemini failed after rotations. Last error: {last_exc}")

def ask_gemini(text, instruction, user_key=None, uid=None):
    model = model_switcher.get_model(uid) if uid is not None else GEMINI_MODEL
    if user_key:
        payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
        data = gemini_api_call(f"models/{model}:generateContent", payload, user_key)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            raise RuntimeError("Unexpected Gemini response")
    else:
        def perform(key):
            payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
            data = gemini_api_call(f"models/{model}:generateContent", payload, key)
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                raise RuntimeError("Unexpected Gemini response")
        return execute_gemini_action(perform)

def transcribe_media_gemini(file_url, mime_type, language_code, user_key=None, uid=None):
    file_content = requests.get(file_url, timeout=REQUEST_TIMEOUT).content
    b64_data = base64.b64encode(file_content).decode('utf-8')
    prompt = """
Transcribe the audio accurately in its original language.

Formatting rules:
- Preserve the original meaning exactly
- Add proper punctuation
- Split the text into short, readable paragraphs
- Each paragraph should represent one clear idea
- Avoid long blocks of text
- Remove filler words only if meaning is unchanged
- Do NOT summarize
- Do NOT add explanations

Return ONLY the final formatted transcription.
"""
    model = model_switcher.get_model(uid) if uid is not None else GEMINI_MODEL
    if user_key:
        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_data
                        }
                    }
                ]
            }]
        }
        data = gemini_api_call(f"models/{model}:generateContent", payload, user_key)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            raise RuntimeError(f"Gemini Transcription Error: {e}")
    else:
        def perform(key):
            payload = {
                "contents": [{
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_data
                            }
                        }
                    ]
                }]
            }
            data = gemini_api_call(f"models/{model}:generateContent", payload, key)
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as e:
                raise RuntimeError(f"Gemini Transcription Error: {e}")
        return execute_gemini_action(perform)

def build_action_keyboard(text_len):
    btns = []
    if text_len > 1000:
        btns.append([InlineKeyboardButton("Get Summarize", callback_data="summarize_menu|")])
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

def ensure_joined(message):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, message.from_user.id)
        if getattr(member, "status", "") in ['member', 'administrator', 'creator']:
            return True
    except:
        pass
    clean = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean}")]])
    bot.reply_to(message, "First, join my channel and come back 👍", reply_markup=kb)
    return False

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if ensure_joined(message):
        welcome_text = (
            "👋 Salaam!\n"
            "• Send me\n"
            "• voice message\n"
            "• audio file\n"
            "• video\n"
            "• to transcribe using Gemini AI\n\n"
            "Select the language spoken in your audio or video:"
        )
        kb = build_lang_keyboard("file")
        bot.reply_to(message, welcome_text, reply_markup=kb, parse_mode="Markdown")

@bot.message_handler(commands=['mode'])
def choose_mode(message):
    if ensure_joined(message):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
            [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
        ])
        bot.reply_to(message, "How do I send you long transcripts?:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('mode|'))
def mode_cb(call):
    if not ensure_joined(call.message):
        return
    mode = call.data.split("|")[1]
    set_user_mode_db(call.from_user.id, mode)
    try:
        bot.edit_message_text(f"you choosed: {mode}", call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    bot.answer_callback_query(call.id, f"Mode set to: {mode} ☑️")

@bot.message_handler(commands=['lang'])
def lang_command(message):
    if ensure_joined(message):
        kb = build_lang_keyboard("file")
        bot.reply_to(message, "Select the language spoken in your audio or video:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('lang|'))
def lang_cb(call):
    try:
        _, code, lbl, origin = call.data.split("|")
    except:
        return
    if origin != "file":
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        process_text_action(call, origin, f"Translate to {lbl}", f"Translate this text in to language {lbl}. No extra text ONLY return the translated text.")
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
    chat_id = call.message.chat.id
    set_user_selected_lang_db(chat_id, code)
    bot.answer_callback_query(call.id, f"Language set: {lbl} ☑️")
    pending = pending_files.pop(chat_id, None)
    if not pending:
        return
    file_url = pending.get("url")
    mime_type = pending.get("mime")
    orig_msg = pending.get("message")
    bot.send_chat_action(chat_id, 'typing')
    try:
        user_key = get_user_key_db(orig_msg.from_user.id)
        text = transcribe_media_gemini(file_url, mime_type, code, user_key=user_key, uid=orig_msg.from_user.id)
        if not text:
            raise ValueError("Empty transcription")
        sent = send_long_text(chat_id, text, orig_msg.message_id, orig_msg.from_user.id)
        if sent:
            user_transcriptions.setdefault(chat_id, {})[sent.message_id] = {"text": text, "origin": orig_msg.message_id}
            if len(text) > 0:
                try:
                    bot.edit_message_reply_markup(chat_id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                except:
                    pass
            if not user_key:
                increment_free_use(orig_msg.from_user.id)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('summarize_menu|'))
def action_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_summarize_keyboard(call.message.message_id))
    except:
        try:
            bot.answer_callback_query(call.id, "Opening summarize options...")
        except:
            pass

def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)

@bot.callback_query_handler(func=lambda c: c.data.startswith('summopt|'))
def summopt_cb(call):
    try:
        _, style, origin = call.data.split("|")
    except:
        bot.answer_callback_query(call.id, "Invalid option", show_alert=True)
        return
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    prompt = ""
    if style == "Short":
        prompt = "Summarize this text in the original language in 1-2 concise sentences. No extra text — return only the summary."
    elif style == "Detailed":
        prompt = "Summarize this text in the original language in a detailed paragraph preserving key points. No extra text — return only the summary."
    else:
        prompt = "Summarize this text in the original language as a bulleted list of main points. No extra text — return only the summary."
    process_text_action(call, origin, f"Summarize ({style})", prompt)

def process_text_action(call, origin_msg_id, log_action, prompt_instr):
    chat_id = call.message.chat.id
    try:
        origin_id = int(origin_msg_id)
    except:
        origin_id = call.message.message_id
    data = user_transcriptions.get(chat_id, {}).get(origin_id)
    if not data:
        if call.message.reply_to_message:
             data = user_transcriptions.get(chat_id, {}).get(call.message.reply_to_message.message_id)
    if not data:
        bot.answer_callback_query(call.id, "Data not found (expired). Resend file.", show_alert=True)
        return
    text = data["text"]
    bot.answer_callback_query(call.id, "Processing...")
    bot.send_chat_action(chat_id, 'typing')
    try:
        user_key = get_user_key_db(call.from_user.id)
        res = ask_gemini(text, prompt_instr, user_key=user_key, uid=call.from_user.id)
        send_long_text(chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        bot.send_message(chat_id, f"Error: {e}")

@bot.message_handler(func=lambda m: isinstance(m.text, str) and m.text.strip().startswith("AIz"))
def set_key_plain(message):
    if not ensure_joined(message):
        return
    token = message.text.strip().split()[0]
    if not token.startswith("AIz"):
        return
    prev = get_user_key_db(message.from_user.id)
    set_user_key_db(message.from_user.id, token)
    msg = "API key updated." if prev else "Okay send me audio or video 👍"
    bot.reply_to(message, msg)
    if not prev:
        try:
            uname = getattr(message.from_user, "username", "N/A")
            uid = message.from_user.id
            info = f"New user provided Gemini key\nUsername: @{uname}\nId: {uid}\nModel: {GEMINI_MODEL}\nFallback: {GEMINI_FALLBACK_MODEL}"
            if ADMIN_ID:
                try:
                    if REQUIRED_CHANNEL:
                        chat_info = bot.get_chat(REQUIRED_CHANNEL)
                        pinned = getattr(chat_info, "pinned_message", None)
                        if pinned:
                            try:
                                bot.forward_message(ADMIN_ID, chat_info.id, pinned.message_id)
                            except:
                                pass
                    bot.send_message(ADMIN_ID, info)
                except:
                    pass
        except:
            pass

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    if not ensure_joined(message):
        return
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        bot.reply_to(message, f"File too large. Gemini inline limit is {MAX_UPLOAD_MB}MB.")
        return
    mime_type = "audio/mp3"
    if message.voice: mime_type = "audio/ogg"
    elif message.audio: mime_type = message.audio.mime_type or "audio/mp3"
    elif message.video: mime_type = message.video.mime_type or "video/mp4"
    elif message.document:
        mime_type = message.document.mime_type or mimetypes.guess_type(message.document.file_name)[0] or "audio/mp3"
    bot.send_chat_action(message.chat.id, 'typing')
    try:
        file_info = bot.get_file(media.file_id)
        telegram_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        lang = user_selected_lang.get(message.chat.id)
        user_key = get_user_key_db(message.from_user.id)
        free_remaining = get_free_uses_remaining(message.from_user.id)
        if not user_key and free_remaining <= 0:
            bot.reply_to(message, "first send me Gemini key 🤓")
            try:
                if REQUIRED_CHANNEL:
                    me = bot.get_me()
                    try:
                        bot_member = bot.get_chat_member(REQUIRED_CHANNEL, me.id)
                        if getattr(bot_member, "status", "") in ['administrator', 'creator']:
                            chat_info = bot.get_chat(REQUIRED_CHANNEL)
                            pinned = getattr(chat_info, "pinned_message", None)
                            if pinned:
                                try:
                                    bot.forward_message(message.chat.id, chat_info.id, pinned.message_id)
                                except:
                                    pass
                    except:
                        pass
            except:
                pass
            pending_files[message.chat.id] = {"url": telegram_file_url, "mime": mime_type, "message": message}
            kb = build_lang_keyboard("file")
            bot.reply_to(message, "Select the language spoken in your audio or video:", reply_markup=kb)
            return
        if not lang:
            pending_files[message.chat.id] = {"url": telegram_file_url, "mime": mime_type, "message": message}
            kb = build_lang_keyboard("file")
            bot.reply_to(message, "Select the language spoken in your audio or video:", reply_markup=kb)
            return
        text = transcribe_media_gemini(telegram_file_url, mime_type, lang, user_key=user_key, uid=message.from_user.id)
        if not text:
            raise ValueError("Empty response")
        sent = send_long_text(message.chat.id, text, message.message_id, message.from_user.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.message_id] = {"text": text, "origin": message.message_id}
            if len(text) > 0:
                try:
                    bot.edit_message_reply_markup(message.chat.id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                except:
                    pass
            if not user_key:
                increment_free_use(message.from_user.id)
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

def send_long_text(chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid)
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                sent = bot.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            sent = bot.send_document(chat_id, open(fname, 'rb'), caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            os.remove(fname)
            return sent
    return bot.send_message(chat_id, text, reply_to_message_id=reply_id)

def _process_webhook_update(raw):
    try:
        upd = Update.de_json(raw.decode('utf-8'))
        bot.process_new_updates([upd])
    except Exception as e:
        logging.exception(f"Error processing update: {e}")

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        data = request.get_data()
        threading.Thread(target=_process_webhook_update, args=(data,), daemon=True).start()
        return '', 200
    abort(403)

if __name__ == "__main__":
    if WEBHOOK_URL:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=WEBHOOK_URL)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Webhook URL not set, exiting.")
