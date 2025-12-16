import os
import threading
import logging
import time
import tempfile
import uuid
import shutil
import zipfile
import urllib.request
import json
import wave
import subprocess
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update
import requests
from vosk import Model, KaldiRecognizer

BOT_TOKEN = os.environ.get("BOT2_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
GEMINI_KEYS = os.environ.get("GEMINI_KEYS", GEMINI_KEY)
GEMINI_MODEL = "gemini-2.5-flash"
VOSK_MODEL_PATH = os.environ.get("VOSK_MODEL_PATH", "./vosk-model-small-en-us-0.15")
VOSK_ZIP_URL = os.environ.get("VOSK_ZIP_URL", "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip")

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

LANGS = [("🇬🇧 English","en")]

user_mode = {}
user_transcriptions = {}
user_selected_lang = {}
pending_files = {}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
flask_app = Flask(__name__)

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

def ask_gemini(text, instruction):
    if not gemini_rotator.keys:
        raise RuntimeError("GEMINI_KEY(s) not configured")
    def perform(key):
        payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
        data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key)
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            raise RuntimeError("Unexpected Gemini response")
    return execute_gemini_action(perform)

def build_lang_keyboard(origin):
    btns = [[InlineKeyboardButton(lbl, callback_data=f"lang|{code}|{lbl}|{origin}") for lbl, code in LANGS]]
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
            "👋 Hello!\n"
            "• Send me a\n"
            "• voice message\n"
            "• audio file\n"
            "• video\n"
            "to transcribe in English."
        )
        kb = build_lang_keyboard("file")
        bot.reply_to(message, welcome_text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('lang|'))
def lang_cb(call):
    _, code, lbl, origin = call.data.split("|")
    chat_id = call.message.chat.id
    user_selected_lang[chat_id] = code
    bot.answer_callback_query(call.id, f"Language set: {lbl} ☑️")
    pending = pending_files.pop(chat_id, None)
    if not pending:
        return
    file_path = pending.get("path")
    orig_msg = pending.get("message")
    bot.send_chat_action(chat_id, 'typing')
    try:
        text = transcribe_file(file_path)
        sent = bot.send_message(chat_id, text, reply_to_message_id=orig_msg.id)
        user_transcriptions.setdefault(chat_id, {})[sent.message_id] = {"text": text, "origin": orig_msg.id}
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

def ensure_vosk_model():
    if os.path.isdir(VOSK_MODEL_PATH):
        return
    zip_path = os.path.join(DOWNLOADS_DIR, "vosk_model.zip")
    urllib.request.urlretrieve(VOSK_ZIP_URL, zip_path)
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(".")
    os.remove(zip_path)

vosk_model = None
try:
    ensure_vosk_model()
    vosk_model = Model(VOSK_MODEL_PATH)
except Exception as e:
    logging.warning(f"Vosk model load failed: {e}")
    vosk_model = None

def transcribe_with_vosk(wav_path):
    wf = wave.open(wav_path, "rb")
    rec = KaldiRecognizer(vosk_model, wf.getframerate())
    rec.SetWords(False)
    result_text = []
    while True:
        data = wf.readframes(4000)
        if len(data) == 0:
            break
        if rec.AcceptWaveform(data):
            res = json.loads(rec.Result())
            if res.get("text"):
                result_text.append(res["text"])
    final_res = json.loads(rec.FinalResult())
    if final_res.get("text"):
        result_text.append(final_res["text"])
    wf.close()
    return " ".join(result_text).strip()

def transcribe_file(file_path):
    tmp_fd, tmp_wav = tempfile.mkstemp(suffix=f"_{uuid.uuid4().hex}.wav", dir=DOWNLOADS_DIR)
    os.close(tmp_fd)
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", file_path,
            "-ar", "16000",
            "-ac", "1",
            "-vn",
            "-f", "wav",
            tmp_wav
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        text = transcribe_with_vosk(tmp_wav)
        return text
    finally:
        if os.path.exists(tmp_wav):
            os.remove(tmp_wav)

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    if not ensure_joined(message):
        return
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        bot.reply_to(message, f"Send a file less than {MAX_UPLOAD_MB}MB")
        return
    bot.send_chat_action(message.chat.id, 'typing')
    file_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{media.file_unique_id}")
    try:
        file_info = bot.get_file(media.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(file_path, 'wb') as f:
            f.write(downloaded)
        lang = user_selected_lang.get(message.chat.id)
        if not lang:
            pending_files[message.chat.id] = {"path": file_path, "message": message}
            kb = build_lang_keyboard("file")
            bot.reply_to(message, "Select the language (English only):", reply_markup=kb)
            return
        text = transcribe_file(file_path)
        bot.send_message(message.chat.id, text, reply_to_message_id=message.id)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([Update.de_json(request.get_data().decode('utf-8'))])
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
