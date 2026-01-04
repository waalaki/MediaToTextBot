import os
import threading
import json
import requests
import logging
import time
import base64
import mimetypes
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
GEMINI_KEYS = os.environ.get("GEMINI_KEYS", "")
FLASH_LITE_KEYS = os.environ.get("FLASH_LITE_KEYS", "")
GEMINI_MODEL_FLASH = os.environ.get("GEMINI_MODEL_FLASH", "gemini-2.5-flash")
GEMINI_MODEL_FLASH_LITE = os.environ.get("GEMINI_MODEL_FLASH_LITE", "gemini-2.5-flash-lite")
KEY_BACKOFF_SECONDS = int(os.environ.get("KEY_BACKOFF_SECONDS", "86400"))

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KeyRotator:
    def __init__(self, keys, backoff=KEY_BACKOFF_SECONDS):
        self.keys = [k.strip() for k in keys.split(",") if k.strip()] if isinstance(keys, str) else list(keys or [])
        self.backoff = backoff
        self.pos = 0
        self.lock = threading.Lock()
        self.disabled_until = {}

    def _is_disabled(self, key):
        ts = self.disabled_until.get(key)
        if ts and time.time() < ts:
            return True
        return False

    def get_key(self):
        with self.lock:
            if not self.keys:
                return None
            tried = 0
            n = len(self.keys)
            while tried < n:
                key = self.keys[self.pos]
                self.pos = (self.pos + 1) % n
                tried += 1
                if not self._is_disabled(key):
                    return key
            return None

    def mark_success(self, key):
        with self.lock:
            if key in self.disabled_until:
                self.disabled_until.pop(key, None)
            if key in self.keys:
                try:
                    i = self.keys.index(key)
                    self.pos = (i + 1) % len(self.keys)
                except ValueError:
                    pass

    def mark_failure(self, key, reason_code=None, backoff_override=None):
        with self.lock:
            backoff = backoff_override if backoff_override is not None else self.backoff
            if reason_code == 429:
                self.disabled_until[key] = time.time() + backoff
            else:
                self.disabled_until[key] = time.time() + min(backoff, 10)

    def any_available(self):
        with self.lock:
            now = time.time()
            for k in self.keys:
                ts = self.disabled_until.get(k)
                if not ts or now >= ts:
                    return True
            return False

flash_rotator = KeyRotator(GEMINI_KEYS)
flash_lite_rotator = KeyRotator(FLASH_LITE_KEYS)

LANGS = [
("🇬🇧 English","en"), ("🇸🇦 العربية","ar"), ("🇪🇸 Español","es"), ("🇫🇷 Français","fr"),
("🇷🇺 Русский","ru"), ("🇩🇪 Deutsch","de"), ("🇮🇳 हिन्दी","hi"), ("🇮🇷 فارسی","fa"),
("🇮🇩 Indonesia","id"), ("🇺🇦 Українська","uk"), ("🇦🇿 Azərbaycan","az"), ("🇮🇹 Italiano","it"),
("🇹🇷 Türkçe","tr"), ("🇧🇬 Български","bg"), ("🇷🇸 Srpski","sr"), ("🇵🇰 اردو","ur"),
("🇹🇭 ไทย","th"), ("🇻🇳 Tiếng Việt","vi"), ("🇯🇵 日本語","ja"), ("🇰🇷 한국어","ko"),
("🇨🇳 中文","zh"), ("🇸🇪 Svenska","sv"), ("🇳🇴 Norsk","no"),
("🇮🇱 עברית","he"), ("🇩🇰 Dansk","da"), ("🇪🇹 አማርኛ","am"), ("🇫🇮 Suomi","fi"),
("🇧🇩 বাংলা","bn"), ("🇰🇪 Kiswahili","sw"), ("🇪🇹 Oromo","om"), ("🇳🇵 नेपाली","ne"),
("🇵🇱 Polski","pl"), ("🇬🇷 Ελληνικά","el"), ("🇨🇿 Čeština","cs"), ("🇮🇸 Íslenska","is"),
("🇱🇹 Lietuvių","lt"), ("🇱🇻 Latviešu","lv"), ("🇭🇷 Hrvatski","hr"), ("🇷🇸 Bosanski","bs"),
("🇭🇺 Magyar","hu"), ("🇷🇴 Română","ro"), ("🇸🇴 Somali","so"), ("🇲🇾 Melayu","ms"),
("🇺🇿 O'zbekcha","uz"), ("🇵🇭 Tagalog","tl"), ("🇵🇹 Português","pt")
]

LANG_MAP = {code: lbl for lbl, code in LANGS}

user_mode = {}
user_transcriptions = {}
action_usage = {}
user_selected_lang = {}
pending_files = {}

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
flask_app = Flask(__name__)

def gemini_api_call(endpoint, payload, key):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def execute_gemini_action(action_callback):
    last_exc = None
    flash_attempted = False
    flash_failed_all = True
    total_flash_keys = len(flash_rotator.keys) or 0
    for _ in range(total_flash_keys if total_flash_keys > 0 else 1):
        key = flash_rotator.get_key()
        if not key:
            break
        flash_attempted = True
        try:
            result = action_callback(key, GEMINI_MODEL_FLASH)
            flash_rotator.mark_success(key)
            return result
        except Exception as e:
            last_exc = e
            code = None
            if isinstance(e, requests.exceptions.HTTPError) and getattr(e, "response", None) is not None:
                try:
                    code = e.response.status_code
                except Exception:
                    code = None
            if code == 429:
                flash_rotator.mark_failure(key, reason_code=429)
            else:
                flash_rotator.mark_failure(key, reason_code=code)
            logging.warning(f"Flash key error {str(key)[:4]}: {e}")
    if flash_attempted and not flash_rotator.any_available():
        flash_failed_all = True
    else:
        flash_failed_all = last_exc is not None
    if flash_failed_all and flash_lite_rotator.keys:
        total_lite = len(flash_lite_rotator.keys) or 0
        for _ in range(total_lite if total_lite > 0 else 1):
            key = flash_lite_rotator.get_key()
            if not key:
                break
            try:
                result = action_callback(key, GEMINI_MODEL_FLASH_LITE)
                flash_lite_rotator.mark_success(key)
                return result
            except Exception as e:
                last_exc = e
                code = None
                if isinstance(e, requests.exceptions.HTTPError) and getattr(e, "response", None) is not None:
                    try:
                        code = e.response.status_code
                    except Exception:
                        code = None
                if code == 429:
                    flash_lite_rotator.mark_failure(key, reason_code=429)
                else:
                    flash_lite_rotator.mark_failure(key, reason_code=code)
                logging.warning(f"Flash-Lite key error {str(key)[:4]}: {e}")
    raise RuntimeError(f"Gemini failed after rotations. Last error: {last_exc}")

def ask_gemini(text, instruction):
    if not flash_rotator.keys and not flash_lite_rotator.keys:
        raise RuntimeError("GEMINI_KEY(s) not configured")
    def perform(key, model):
        payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
        data = gemini_api_call(f"models/{model}:generateContent", payload, key)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            raise RuntimeError("Unexpected Gemini response")
    return execute_gemini_action(perform)

def transcribe_media_gemini(file_url, mime_type, target_lang_label):
    if not flash_rotator.keys and not flash_lite_rotator.keys:
        raise RuntimeError("GEMINI_KEY not configured")
    file_content = requests.get(file_url, timeout=REQUEST_TIMEOUT).content
    b64_data = base64.b64encode(file_content).decode('utf-8')
    prompt = f"""
You are a professional transcription and translation system.
Step 1: Detect the spoken language automatically.
Step 2: Transcribe the audio accurately.
Step 3: Translate the transcription into {target_lang_label}.
Formatting rules:
- Final output MUST be written ONLY in {target_lang_label}
- Preserve the original meaning exactly
- Add proper punctuation
- Split the text into short, readable paragraphs
- Each paragraph should represent one clear idea
- Avoid long blocks of text
- Remove filler words only if meaning is unchanged
- Do NOT summarize
- Do NOT add explanations
Return ONLY the final formatted transcription in {target_lang_label}.
"""
    def perform(key, model):
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
    btns, row = [], []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}"))
        if i % 3 == 0:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    return InlineKeyboardMarkup(btns)

def build_summarize_keyboard(origin):
    btns = [
        [InlineKeyboardButton("Short", callback_data=f"summopt|Short|{origin}")],
        [InlineKeyboardButton("Detailed", callback_data=f"summopt|Detailed|{origin}")],
        [InlineKeyboardButton("Bulleted", callback_data=f"summopt|Bulleted|{origin}")]
    ]
    return InlineKeyboardMarkup(btns)

def ensure_joined(message):
    if not REQUIRED_CHANNEL:
        return True
    try:
        if bot.get_chat_member(REQUIRED_CHANNEL, message.from_user.id).status in ['member', 'administrator', 'creator']:
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
    user_mode[call.from_user.id] = mode
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
    _, code, lbl, origin = call.data.split("|")
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
    user_selected_lang[chat_id] = code
    bot.answer_callback_query(call.id, f"Language set: {lbl} ☑️")
    pending = pending_files.pop(chat_id, None)
    if not pending:
        return
    file_url = pending.get("url")
    mime_type = pending.get("mime")
    orig_msg = pending.get("message")
    bot.send_chat_action(chat_id, 'typing')
    try:
        lang_label = LANG_MAP.get(code, lbl)
        text = transcribe_media_gemini(file_url, mime_type, lang_label)
        if not text:
            raise ValueError("Empty transcription")
        sent = send_long_text(chat_id, text, orig_msg.id, orig_msg.from_user.id)
        if sent:
            user_transcriptions.setdefault(chat_id, {})[sent.message_id] = {"text": text, "origin": orig_msg.id}
            if len(text) > 0:
                try:
                    bot.edit_message_reply_markup(chat_id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                except:
                    pass
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith('summarize_menu|'))
def action_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_summarize_keyboard(call.message.id))
    except:
        try:
            bot.answer_callback_query(call.id, "Opening summarize options...")
        except:
            pass

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
    chat_id = call.message.chat.id
    user_code = user_selected_lang.get(chat_id)
    user_label = LANG_MAP.get(user_code, "English") if user_code else "the original language"
    if style == "Short":
        prompt = f"Summarize this text in {user_label} in 1-2 concise sentences. No extra text — return only the summary."
    elif style == "Detailed":
        prompt = f"Summarize this text in {user_label} in a detailed paragraph preserving key points. No extra text — return only the summary."
    else:
        prompt = f"Summarize this text in {user_label} as a bulleted list of main points. No extra text — return only the summary."
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
        res = ask_gemini(text, prompt_instr)
        send_long_text(chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        bot.send_message(chat_id, f"Error: {e}")

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
        lang_code = user_selected_lang.get(message.chat.id)
        if not lang_code:
            pending_files[message.chat.id] = {"url": telegram_file_url, "mime": mime_type, "message": message}
            kb = build_lang_keyboard("file")
            bot.reply_to(message, "Select the language spoken in your audio or video:", reply_markup=kb)
            return
        lang_label = LANG_MAP.get(lang_code, lang_code)
        text = transcribe_media_gemini(telegram_file_url, mime_type, lang_label)
        if not text:
            raise ValueError("Empty response")
        sent = send_long_text(message.chat.id, text, message.id, message.from_user.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.message_id] = {"text": text, "origin": message.id}
            if len(text) > 0:
                try:
                    bot.edit_message_reply_markup(message.chat.id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
                except:
                    pass
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

def send_long_text(chat_id, text, reply_id, uid, action="Transcript"):
    mode = user_mode.get(uid, "📄 Text File")
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
