import os
import threading
import json
import requests
import logging
import time
import subprocess
import re
import tempfile
import io
from flask import Flask, request, abort, render_template_string, jsonify
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update
from itsdangerous import URLSafeSerializer, SignatureExpired, BadSignature

FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "/usr/bin/ffmpeg")
BOT_TOKEN = os.environ.get("BOT2_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_WEB_UPLOAD_MB = int(os.environ.get("MAX_WEB_UPLOAD_MB", "250"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_WEB_UPLOAD_SIZE = MAX_WEB_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
MAX_AUDIO_DURATION_SEC = 9 * 60 * 60
DEFAULT_GEMINI_KEYS = os.environ.get("DEFAULT_GEMINI_KEYS", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_KEYS = os.environ.get("GEMINI_API_KEYS", DEFAULT_GEMINI_KEYS)
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
GEMINI_MODEL_FALLBACKS = [m.strip() for m in os.environ.get("GEMINI_MODEL_FALLBACKS", GEMINI_MODEL).split(",") if m.strip()]
SECRET_KEY = os.environ.get("SECRET_KEY", "super_secret_upload_key")

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

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
flask_app = Flask(__name__)
serializer = URLSafeSerializer(SECRET_KEY)

WEB_UPLOAD_HTML_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Upload File</title><style>body{font-family:Arial,sans-serif;background-color:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;color:#333}.container{background-color:#fff;padding:30px;border-radius:12px;box-shadow:0 4px 15px rgba(0,0,0,0.1);text-align:center;width:90%;max-width:500px;box-sizing:border-box}h2{margin-top:0;color:#555;font-size:1.5rem}p{font-size:0.9rem;color:#666;margin-bottom:20px}.file-upload-wrapper{position:relative;overflow:hidden;display:inline-block;cursor:pointer;width:100%}.file-upload-input{position:absolute;left:0;top:0;opacity:0;cursor:pointer;font-size:100px;width:100%;height:100%}.file-upload-label{background-color:#007bff;color:#fff;padding:12px 20px;border-radius:8px;transition:background-color 0.3s;display:block;font-size:1rem}.file-upload-label:hover{background-color:#0056b3}#file-name{margin-top:15px;font-style:italic;color:#777;font-size:0.9rem;word-wrap:break-word;overflow-wrap:break-word;min-height:20px}#progress-bar-container{width:100%;background-color:#e0e0e0;border-radius:5px;margin-top:20px;display:none}#progress-bar{width:0%;height:15px;background-color:#28a745;border-radius:5px;text-align:center;color:white;line-height:15px;transition:width 0.3s ease}#status-message{margin-top:15px;font-weight:bold}.loading-spinner{display:none;width:40px;height:40px;border:4px solid #f3f3f3;border-top:4px solid #007bff;border-radius:50%;animation:spin 1s linear infinite;margin:20px auto}@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}@media (max-width:600px){.container{padding:20px}}</style></head><body><div class="container"><h2>Upload Your Audio/Video</h2><p>Your file is too big for Telegram, but you can upload it here. Max size: {{ max_mb }}MB.</p><div class="file-upload-wrapper"><input type="file" id="file-input" class="file-upload-input" accept=".mp3,.wav,.m4a,.ogg,.webm,.flac,.mp4,.mkv,.avi,.mov,.hevc,.aac,.aiff,.amr,.wma,.opus,.m4v,.ts,.flv,.3gp"><label for="file-input" class="file-upload-label"><span id="upload-text">Choose File to Upload</span></label></div><div id="file-name"></div><div id="progress-bar-container"><div id="progress-bar">0%</div></div><div id="status-message"></div><div class="loading-spinner" id="spinner"></div></div><script>const fileInput=document.getElementById('file-input');const fileNameDiv=document.getElementById('file-name');const progressBarContainer=document.getElementById('progress-bar-container');const progressBar=document.getElementById('progress-bar');const statusMessageDiv=document.getElementById('status-message');const spinner=document.getElementById('spinner');const uploadTextSpan=document.getElementById('upload-text');const MAX_SIZE_MB={{ max_mb }};fileInput.addEventListener('change',function(){if(this.files.length>0){const file=this.files[0];fileNameDiv.textContent=`Selected: ${file.name}`;statusMessageDiv.textContent='';progressBarContainer.style.display='none';progressBar.style.width='0%';progressBar.textContent='0%';if(file.size>MAX_SIZE_MB*1024*1024){statusMessageDiv.style.color='red';statusMessageDiv.textContent=`Error: File size exceeds the maximum limit of ${MAX_SIZE_MB}MB.`;uploadTextSpan.textContent='Choose File to Upload';fileNameDiv.textContent=''}else{uploadFile(file)}}});function uploadFile(file){const formData=new FormData();formData.append('file',file);const xhr=new XMLHttpRequest();xhr.open('POST',window.location.href);xhr.upload.addEventListener('progress',function(e){if(e.lengthComputable){const percent=Math.round((e.loaded/e.total)*100);progressBarContainer.style.display='block';progressBar.style.width=percent+'%';progressBar.textContent=percent+'%';statusMessageDiv.textContent=`Uploading... ${percent}%`;if(percent===100){statusMessageDiv.textContent='Upload complete. Processing...';spinner.style.display='block'}}});xhr.onload=function(){spinner.style.display='none';if(xhr.status===200){statusMessageDiv.style.color='#28a745';statusMessageDiv.textContent='Success! Your transcript will be sent to your Telegram chat shortly.'}else{statusMessageDiv.style.color='red';statusMessageDiv.textContent=`Error: ${xhr.responseText||'An unknown error occurred.'}`}};xhr.onerror=function(){spinner.style.display='none';statusMessageDiv.style.color='red';statusMessageDiv.textContent='Network error. Please try again.'};xhr.send(formData);}</script></body></html>"""

def get_user_mode(uid):
    return user_mode.get(uid, "📄 Text File")

def convert_to_wav(input_path: str) -> str:
    if not FFMPEG_BINARY:
        raise RuntimeError("FFmpeg binary not found.")
    output_path = os.path.join(DOWNLOADS_DIR, f"{os.path.basename(input_path).split('.')[0]}_converted.wav")
    command = [FFMPEG_BINARY, "-i", input_path, "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000", output_path, "-y"]
    subprocess.run(command, check=True, capture_output=True, timeout=REQUEST_TIMEOUT_GEMINI)
    return output_path

API_KEY_PATTERN = re.compile(r"(AIza[0-9A-Za-z\-_]{35})")
URL_WITH_KEY_PATTERN = re.compile(r"(https?://[^\s]*\?key=)[0-9A-Za-z\-_]+")

def sanitize_text_for_user(text: str) -> str:
    if not text:
        return text
    text = URL_WITH_KEY_PATTERN.sub(r"\1<REDACTED_KEY>", text)
    text = API_KEY_PATTERN.sub("<REDACTED_KEY>", text)
    text = re.sub(r"[A-Za-z0-9\-_]{40,}", "<REDACTED>", text)
    return text

def sanitize_exception_message(exc: Exception) -> str:
    msg = str(exc)
    safe = sanitize_text_for_user(msg)
    return safe[:300] if len(safe) > 300 else safe

def gemini_api_call(endpoint, payload, key, headers=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/{endpoint}?key={key}"
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_GEMINI)
        if not resp.ok:
            try:
                resp_json = resp.json()
                err_msg = resp_json.get("error", {}).get("message") or resp.text
            except Exception:
                err_msg = resp.text
            safe_err = sanitize_text_for_user(err_msg)
            logging.warning(f"Gemini API returned HTTP {resp.status_code}: {safe_err}")
            raise RuntimeError(f"Gemini API error ({resp.status_code}): {safe_err}")
        return resp.json()
    except requests.exceptions.RequestException as rexc:
        logging.warning(f"Network error calling Gemini (key={str(key)[:6]}...): {rexc}")
        raise RuntimeError("Network error when contacting Gemini API.") from None

def execute_gemini_action(action_callback):
    while True:
        last_exc = None
        for model in GEMINI_MODEL_FALLBACKS:
            global GEMINI_MODEL
            prev_model = GEMINI_MODEL
            GEMINI_MODEL = model
            keys_count = len(gemini_rotator.keys)
            if keys_count == 0:
                logging.error("No API keys configured. Waiting 30s...")
                time.sleep(30)
                continue
            keys_to_try = max(1, keys_count)
            for _ in range(keys_to_try):
                key = gemini_rotator.get_key()
                if not key:
                    last_exc = RuntimeError("No Gemini keys available")
                    break
                try:
                    result = action_callback(key)
                    gemini_rotator.mark_success(key)
                    GEMINI_MODEL = prev_model
                    return result
                except Exception as e:
                    last_exc = e
                    gemini_rotator.mark_failure(key)
                    continue
        logging.info("All keys and models failed. Retrying cycle in 2 seconds...")
        time.sleep(2)

def upload_and_transcribe_gemini(file_path: str) -> str:
    original_path, converted_path = file_path, None
    if os.path.splitext(file_path)[1].lower() not in [".wav", ".mp3", ".aiff", ".aac", ".ogg", ".flac"]:
        converted_path = convert_to_wav(file_path)
        file_path = converted_path
    file_size = os.path.getsize(file_path)
    mime_type = "audio/wav"
    def perform_upload_and_transcribe(key):
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
                raise RuntimeError("Upload failed.")
            prompt = "Transcribe the audio in this file. Automatically detect the language and provide a clean transcription. Do not add intro phrases."
            payload = {"contents": [{"parts": [{"fileData": {"mimeType": mime_type, "fileUri": uploaded_uri}}, {"text": prompt}]}]}
            data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key, headers={"Content-Type": "application/json"})
            return data["candidates"][0]["content"]["parts"][0]["text"]
        finally:
            if uploaded_name:
                try:
                    requests.delete(f"https://generativelanguage.googleapis.com/v1beta/{uploaded_name}?key={key}", timeout=5)
                except:
                    pass
    try:
        return execute_gemini_action(perform_upload_and_transcribe)
    finally:
        if converted_path and os.path.exists(converted_path):
            os.remove(converted_path)

def ask_gemini(text, instruction):
    def perform_text_query(key):
        payload = {"contents": [{"parts": [{"text": f"{instruction}\n\n{text}"}]}]}
        data = gemini_api_call(f"models/{GEMINI_MODEL}:generateContent", payload, key, headers={"Content-Type": "application/json"})
        return data["candidates"][0]["content"]["parts"][0]["text"]
    return execute_gemini_action(perform_text_query)

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

def ensure_joined(message):
    if not REQUIRED_CHANNEL:
        return True
    try:
        if bot.get_chat_member(REQUIRED_CHANNEL, message.from_user.id).status in ['member', 'administrator', 'creator']:
            return True
    except:
        pass
    clean = REQUIRED_CHANNEL.replace("@", "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Click here Subscribe 💋", url=f"https://t.me/{clean}")]])
    bot.reply_to(message, "First subscribe to my channel and come back 👍", reply_markup=kb)
    return False

def signed_upload_token(chat_id, message_id):
    return serializer.dumps({"chat_id": chat_id, "message_id": message_id})

def unsign_upload_token(token):
    try:
        return serializer.loads(token)
    except (SignatureExpired, BadSignature):
        raise BadSignature("Invalid or expired link.")

def process_uploaded_file(chat_id, original_msg_id, file_path):
    try:
        text = upload_and_transcribe_gemini(file_path)
        if not text:
            raise ValueError("Empty response")
        sent = send_long_text(chat_id, text, original_msg_id, chat_id)
        if sent:
            user_transcriptions.setdefault(chat_id, {})[sent.message_id] = {"text": text, "origin": original_msg_id}
            bot.edit_message_reply_markup(chat_id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
    except Exception as e:
        safe = sanitize_exception_message(e)
        bot.send_message(chat_id, f"❌ Error transcribing web upload: {safe}", reply_to_message_id=original_msg_id)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if ensure_joined(message):
        welcome_text = (
            "👋 Salaam!\n"
            "• Send me\n"
            "• voice message\n"
            "• audio file\n"
            "• video\n"
            "• to transcribe for free"
        )
        bot.reply_to(message, welcome_text, parse_mode="Markdown")

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

@bot.callback_query_handler(func=lambda c: c.data.startswith('lang|'))
def lang_cb(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    _, code, lbl, origin = call.data.split("|")
    process_text_action(call, origin, f"Translate to {lbl}", f"Translate this text in to language {lbl}. No extra text ONLY return the translated text.")

@bot.callback_query_handler(func=lambda c: c.data.startswith(('translate_menu|', 'summarize|')))
def action_cb(call):
    action, _ = call.data.split("|")
    if action == "translate_menu":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_lang_keyboard("trans"))
    else:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        process_text_action(call, call.message.message_id, "Summarize", "Summarize this in original language.")

def process_text_action(call, origin_msg_id, log_action, prompt_instr):
    if not ensure_joined(call.message):
        return
    chat_id, msg_id = call.message.chat.id, call.message.message_id
    data = user_transcriptions.get(chat_id, {}).get(msg_id)
    if not data:
        bot.answer_callback_query(call.id, "Data not found (expired). Resend file.", show_alert=True)
        return
    text = data["text"]
    key = f"{chat_id}|{msg_id}|{log_action}"
    if "Summarize" in log_action and action_usage.get(key, 0) >= 1:
        bot.answer_callback_query(call.id, "Already summarized!", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Processing...")
    bot.send_chat_action(chat_id, 'typing')
    try:
        res = ask_gemini(text, prompt_instr)
        action_usage[key] = action_usage.get(key, 0) + 1
        send_long_text(chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception as e:
        safe = sanitize_exception_message(e)
        bot.send_message(chat_id, f"Error: {safe}")

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    if not ensure_joined(message):
        return
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        token = signed_upload_token(message.chat.id, message.message_id)
        upload_link = f"{WEBHOOK_URL_BASE.rstrip('/')}/upload/{token}"
        
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬆️ Upload file (Max {MAX_WEB_UPLOAD_MB}MB)", url=upload_link)]])
        
        bot.reply_to(message, 
                     f"File size exceeds Telegram limit ({MAX_UPLOAD_MB}MB). Use the button below to upload your file via the web interface. Max file size: {MAX_WEB_UPLOAD_MB}MB.", 
                     reply_markup=kb, 
                     parse_mode="HTML")
        return

    bot.send_chat_action(message.chat.id, 'typing')
    file_path = os.path.join(DOWNLOADS_DIR, f"temp_{message.id}_{media.file_unique_id}")
    try:
        file_info = bot.get_file(media.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(file_path, 'wb') as f:
            f.write(downloaded)
        text = upload_and_transcribe_gemini(file_path)
        if not text:
            raise ValueError("Empty response")
        sent = send_long_text(message.chat.id, text, message.id, message.from_user.id)
        if sent:
            user_transcriptions.setdefault(message.chat.id, {})[sent.message_id] = {"text": text, "origin": message.id}
            bot.edit_message_reply_markup(message.chat.id, sent.message_id, reply_markup=build_action_keyboard(len(text)))
    except Exception as e:
        safe = sanitize_exception_message(e)
        bot.reply_to(message, f"❌ Error: {safe}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

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

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([Update.de_json(request.get_data().decode('utf-8'))])
        return '', 200
    abort(403)

@flask_app.route("/upload/<token>", methods=['GET', 'POST'])
def upload_large_file(token):
    try:
        data = unsign_upload_token(token)
    except BadSignature:
        return "<h3>Invalid or expired upload link. Please try sending the file to the bot again.</h3>", 400

    chat_id = data.get("chat_id")
    original_msg_id = data.get("message_id")

    if request.method == 'GET':
        return render_template_string(WEB_UPLOAD_HTML_TEMPLATE, max_mb=MAX_WEB_UPLOAD_MB)

    file = request.files.get('file')
    if not file:
        return "No file uploaded", 400

    file_bytes = file.read()
    if len(file_bytes) > MAX_WEB_UPLOAD_SIZE:
        return f"File too large. Max allowed is {MAX_WEB_UPLOAD_MB}MB.", 400

    def bytes_to_tempfile(b, filename):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ".tmp"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir=DOWNLOADS_DIR)
        tmp.write(b)
        tmp.flush()
        tmp.close()
        return tmp.name

    tmp_path = bytes_to_tempfile(file_bytes, file.filename or "upload_file")

    # Start processing in a separate thread
    threading.Thread(target=process_uploaded_file, args=(chat_id, original_msg_id, tmp_path)).start()

    return jsonify({"status": "accepted", "message": "Upload accepted. Processing started. Your transcription will be sent to your Telegram chat when ready."})


if __name__ == "__main__":
    if WEBHOOK_URL:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=WEBHOOK_URL)
        flask_app.run(host="0.0.0.0", port=PORT)
    else:
        print("Webhook URL not set, exiting.")
