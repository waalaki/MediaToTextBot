import os
import threading
import json
import requests
import logging
import time
import subprocess
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telebot import apihelper

FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "/usr/bin/ffmpeg")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT2_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "250"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
MAX_AUDIO_DURATION_SEC = 9 * 60 * 60
DEFAULT_GEMINI_KEYS = os.environ.get("DEFAULT_GEMINI_KEYS", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEYS = os.environ.get("GEMINI_API_KEYS", DEFAULT_GEMINI_KEYS)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class KeyRotator:
    def __init__(self, keys):
        self.keys = [k.strip() for k in keys.split(",") if k.strip()]
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

gemini_rotator = KeyRotator(GEMINI_API_KEYS)

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

def get_user_mode(uid, default="📄 Text File"):
    return user_mode.get(uid, default)

def convert_to_wav(input_path: str) -> str:
    if not FFMPEG_BINARY:
        raise RuntimeError("FFmpeg binary not found.")
    output_path = os.path.join(DOWNLOADS_DIR, f"{os.path.basename(input_path).split('.')[0]}_converted.wav")
    command = [FFMPEG_BINARY, "-i", input_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", output_path, "-y"]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path

def gemini_api_call(endpoint, payload, key, timeout, headers=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()

def upload_and_transcribe_gemini(file_path: str) -> str:
    original_path = file_path
    converted_path = None
    if os.path.splitext(file_path)[1].lower() not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
        converted_path = convert_to_wav(file_path)
        file_path = converted_path
    file_size = os.path.getsize(file_path)
    mime_type = "audio/wav"
    last_exc = None
    for _ in range(len(gemini_rotator.keys) + 1):
        key = gemini_rotator.get_key()
        if not key:
            raise RuntimeError("No Gemini keys available")
        uploaded_file_name = None
        try:
            upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={key}"
            headers = {"X-Goog-Upload-Protocol": "raw", "X-Goog-Upload-Command": "start, upload, finalize",
                       "X-Goog-Upload-Header-Content-Length": str(file_size), "Content-Type": mime_type}
            with open(file_path, 'rb') as f:
                upload_data = requests.post(upload_url, headers=headers, data=f.read(), timeout=REQUEST_TIMEOUT_GEMINI).json()
            uploaded_file_name = upload_data.get("name", upload_data.get("file", {}).get("name"))
            uploaded_file_uri = upload_data.get("uri", upload_data.get("file", {}).get("uri"))
            if not uploaded_file_name:
                raise RuntimeError("File upload response malformed.")
            prompt = "Transcribe the audio in this file. Automatically detect the language and provide a clean, accurate transcription in the original language of the audio. Do not add any introductory phrases or explanations."
            payload = {"contents": [{"parts": [{"fileData": {"mimeType": mime_type, "fileUri": uploaded_file_uri}}, {"text": prompt}]}]}
            response_data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key, REQUEST_TIMEOUT_GEMINI, headers={"Content-Type": "application/json"})
            text = response_data["candidates"][0]["content"]["parts"][0]["text"]
            gemini_rotator.mark_success(key)
            return text
        except Exception as e:
            last_exc = e
            logging.warning("Gemini error with key %s: %s", str(key)[:4], e)
            gemini_rotator.mark_failure(key)
        finally:
            if uploaded_file_name:
                try:
                    requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{uploaded_file_name}?key={key}", timeout=10)
                except Exception:
                    pass
            if converted_path and os.path.exists(converted_path):
                os.remove(converted_path)
    raise RuntimeError(f"Gemini failed after all key rotations. Last error: {last_exc}")

def ask_gemini(text, instruction, timeout=REQUEST_TIMEOUT_GEMINI):
    for _ in range(len(gemini_rotator.keys) + 1):
        key = gemini_rotator.get_key()
        if not key:
            raise RuntimeError("No GEMINI keys available for text processing")
        try:
            payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
            response_data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key, timeout, headers={"Content-Type": "application/json"})
            gemini_rotator.mark_success(key)
            return response_data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logging.warning("Gemini text error with key %s: %s", str(key)[:4], e)
            gemini_rotator.mark_failure(key)
    raise RuntimeError("Gemini text processing failed.")

def build_action_keyboard(text_length):
    buttons = [[InlineKeyboardButton("⭐️ Get translating", callback_data=f"translate_menu|")]]
    if text_length > 1000:
        buttons.append([InlineKeyboardButton("Summarize", callback_data=f"summarize|")])
    return InlineKeyboardMarkup(buttons)

def build_language_keyboard(origin):
    buttons, row = [], []
    for i, (label, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(label, callback_data=f"lang|{code}|{label}|{origin}"))
        if i % 3 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
flask_app = Flask(__name__)

WELCOME_MESSAGE = """👋 **Salaam!**
• Send me
• **voice message**
• **audio file**
• **video**
• to transcribe for free
"""
HELP_MESSAGE = f"""/start - Show welcome message
/help - This help message
/mode - Choose output format
Send a voice/audio/video (up to {MAX_UPLOAD_MB}MB) to transcribe
"""

def is_user_in_channel(user_id):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator', 'restricted']
    except Exception:
        return False

def send_join_prompt(message):
    if not REQUIRED_CHANNEL:
        return
    clean_channel_username = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url=f"https://t.me/{clean_channel_username}")]])
    bot.reply_to(message, "First, join my channel 😜", reply_markup=kb)

def ensure_joined(message):
    if not REQUIRED_CHANNEL:
        return True
    if is_user_in_channel(message.from_user.id):
        return True
    send_join_prompt(message)
    return False

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not ensure_joined(message):
        return
    if message.text == '/start':
        bot.reply_to(message, WELCOME_MESSAGE, parse_mode="Markdown")
    elif message.text == '/help':
        bot.reply_to(message, HELP_MESSAGE, parse_mode="Markdown")

@bot.message_handler(commands=['mode'])
def choose_mode(message):
    if not ensure_joined(message):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Split messages", callback_data="mode|Split messages")],
        [InlineKeyboardButton("📄 Text File", callback_data="mode|Text File")]
    ])
    bot.reply_to(message, "Choose **output mode**:", reply_markup=keyboard, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('mode|'))
def mode_callback_query(call):
    if not ensure_joined(call.message):
        bot.answer_callback_query(call.id, "🚫 First join my channel", show_alert=True)
        return
    mode_name = call.data.split("|")[1]
    user_mode[call.from_user.id] = mode_name
    bot.answer_callback_query(call.id, f"Mode set to: {mode_name}")
    bot.edit_message_text(f"Output mode set to: **{mode_name}**", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('lang|'))
def language_callback_query(call):
    if not ensure_joined(call.message):
        bot.answer_callback_query(call.id, "🚫 First join my channel", show_alert=True)
        return
    _, code, label, origin = call.data.split("|")
    bot.answer_callback_query(call.id, f"Translating to {label}...")
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    transcription_data = user_transcriptions.get(chat_id, {}).get(message_id)
    if not transcription_data:
        bot.send_message(chat_id, "Original transcription data not found.")
        bot.delete_message(chat_id, message_id)
        return
    original_text = transcription_data["text"]
    bot.delete_message(chat_id, message_id)
    bot.send_chat_action(chat_id, 'typing')
    instruction = f"Translate this text into {label}. Do not add any introductory phrases, or the original text. ONLY return the translated text."
    try:
        translated_text = ask_gemini(original_text, instruction)
        send_long_text(bot, chat_id, translated_text, transcription_data["origin"], call.from_user.id)
    except Exception as e:
        bot.send_message(chat_id, f"Translation error: {e}", reply_to_message_id=transcription_data["origin"])

@bot.callback_query_handler(func=lambda call: call.data.startswith(('translate_menu|', 'summarize|')))
def action_callback_query(call):
    if not ensure_joined(call.message):
        bot.answer_callback_query(call.id, "🚫 First join my channel", show_alert=True)
        return
    action, _ = call.data.split("|")
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    transcription_data = user_transcriptions.get(chat_id, {}).get(message_id)
    if not transcription_data:
        bot.answer_callback_query(call.id, "Transcription not found. Please resend the message.", show_alert=True)
        return
    if action == "translate_menu":
        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=build_language_keyboard("trans"))
        return
    original_text = transcription_data["text"]
    key = f"{chat_id}|{message_id}|{action}"
    if action_usage.get(key, 0) >= 1:
        bot.answer_callback_query(call.id, f"{action.capitalize()} unavailable (maybe expired or Used)", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Processing...", show_alert=False)
    bot.send_chat_action(chat_id, 'typing')
    instruction = "What is this report and what is it about? Please summarize them for me into the original language of the text without adding any introductions, notes, or extra phrases."
    try:
        processed_text = ask_gemini(original_text, instruction)
        action_usage[key] = action_usage.get(key, 0) + 1
        send_long_text(bot, chat_id, processed_text, transcription_data["origin"], call.from_user.id, action)
    except RuntimeError as e:
        bot.send_message(chat_id, f"❌ Error: {e}", reply_to_message_id=transcription_data["origin"])
    except Exception as e:
        logging.error("Action callback error: %s", e)
        bot.send_message(chat_id, f"❌ Unexpected error: {e}", reply_to_message_id=transcription_data["origin"])

def get_file_info(message):
    if message.voice:
        media = message.voice
    elif message.audio:
        media = message.audio
    elif message.video:
        media = message.video
    elif message.document:
        media = message.document
    else:
        return None, None
    size = media.file_size
    duration = getattr(media, 'duration', 0)
    if size > MAX_UPLOAD_SIZE:
        bot.reply_to(message, f"Just Send me a file less than {MAX_UPLOAD_MB}MB 😎")
        return None, None
    if duration > MAX_AUDIO_DURATION_SEC:
        hours = MAX_AUDIO_DURATION_SEC // 3600
        bot.reply_to(message, f"Bot-ka ma aqbalayo cod ka dheer {hours} saac. Fadlan soo dir mid ka gaaban.")
        return None, None
    return bot.get_file(media.file_id), media

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    if not ensure_joined(message):
        return
    file_info, media = get_file_info(message)
    if not file_info:
        return
    bot.send_chat_action(message.chat.id, 'typing')
    file_path = os.path.join(DOWNLOADS_DIR, file_info.file_path.split('/')[-1])
    try:
        downloaded_file = bot.download_file(file_info.file_path)
        with open(file_path, 'wb') as f:
            f.write(downloaded_file)
        text = upload_and_transcribe_gemini(file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Transcription error: {e}")
        return
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    if not text or text.startswith("Error:"):
        warning_text = text or "⚠️ Warning Make sure the voice is clear."
        bot.reply_to(message, warning_text)
        return
    sent_message = send_long_text(bot, message.chat.id, text, message.id, message.from_user.id)
    if sent_message:
        keyboard = build_action_keyboard(len(text))
        user_transcriptions.setdefault(sent_message.chat.id, {})[sent_message.message_id] = {"text": text, "origin": message.id}
        if len(text) > 1000:
            action_usage[f"{sent_message.chat.id}|{sent_message.message_id}|summarize"] = 0
        bot.edit_message_reply_markup(sent_message.chat.id, sent_message.message_id, reply_markup=keyboard)

def send_long_text(bot, chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid, "📄 Text File")
    sent_message = None
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            for part in [text[i:i+MAX_MESSAGE_CHUNK] for i in range(0, len(text), MAX_MESSAGE_CHUNK)]:
                bot.send_chat_action(chat_id, 'typing')
                sent_message = bot.send_message(chat_id, part, reply_to_message_id=reply_id)
        else:
            file_name = os.path.join(DOWNLOADS_DIR, f"{action}.txt")
            with open(file_name, "w", encoding="utf-8") as f:
                f.write(text)
            bot.send_chat_action(chat_id, 'upload_document')
            caption_text = "Open this file and copy the text inside 👍" if action != "Transcript" else "Transcript: Open this file and copy the text inside 👍"
            sent_message = bot.send_document(chat_id, open(file_name, 'rb'), caption=caption_text, reply_to_message_id=reply_id)
            os.remove(file_name)
    else:
        bot.send_chat_action(chat_id, 'typing')
        sent_message = bot.send_message(chat_id, text, reply_to_message_id=reply_id)
    return sent_message

@flask_app.route("/", methods=["GET", "POST", "HEAD"])
def keep_alive_flask():
    return "Bot is alive (Flask/Telebot) ✅", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook_handler():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    else:
        abort(403)

def run_webhook_bot():
    if not WEBHOOK_URL:
        logging.error("WEBHOOK_URL is not set. Cannot run in webhook mode.")
        return
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info("Webhook set successfully to %s", WEBHOOK_URL)
        flask_app.run(host="0.0.0.0", port=PORT, debug=False)
    except Exception as e:
        logging.error("Failed to set up Flask/Webhook: %s", e)

if __name__ == "__main__":
    run_webhook_bot()
