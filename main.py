import os
import threading
import json
import requests
import logging
import time
import tempfile
import mimetypes
import subprocess
import shutil
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update
from queue import Queue

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL_BASE = os.environ.get("WEBHOOK_URL_BASE", "")
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook/")
WEBHOOK_URL = WEBHOOK_URL_BASE.rstrip('/') + WEBHOOK_PATH if WEBHOOK_URL_BASE else ""
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "500"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_MB * 1024 * 1024
MAX_MESSAGE_CHUNK = 4095
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "./downloads")
GROQ_KEYS = os.environ.get("GROQ_KEYS", os.environ.get("GROQ_API_KEY", ""))
GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3")
GROQ_CHAT_MODEL = os.environ.get("GROQ_CHAT_MODEL", "llama-3.1-8b-instant")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "3"))
CHUNK_SECONDS = int(os.environ.get("CHUNK_SECONDS", str(30 * 60)))

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

groq_rotator = KeyRotator(GROQ_KEYS)

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
("🇺🇿 O'zbekcha","uz"), ("🇵🇭 Tagalog","tl"), ("🇵🇹 Português","pt"), ("🔄Auto","auto")
]

user_mode = {}
user_transcriptions = {}
action_usage = {}
user_selected_lang = {}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
flask_app = Flask(__name__)

def get_user_mode(uid):
    return user_mode.get(uid, "Split messages")

def execute_groq_action(action_callback):
    last_exc = None
    total = len(groq_rotator.keys) or 1
    for _ in range(total + 1):
        key = groq_rotator.get_key()
        if not key:
            raise RuntimeError("No GROQ keys available")
        try:
            result = action_callback(key)
            groq_rotator.mark_success(key)
            return result
        except Exception as e:
            last_exc = e
            logging.warning("Groq error with key %s: %s", str(key)[:4], e)
            groq_rotator.mark_failure(key)
    raise RuntimeError("GROQ failed after rotations. Last error: %s" % last_exc)

def convert_to_mp3(input_path, timeout=120):
    fd, out_path = tempfile.mkstemp(suffix=".mp3", dir=DOWNLOADS_DIR)
    os.close(fd)
    try:
        cmd = ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", "-b:a", "48k", out_path]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return out_path
    except Exception:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except:
            pass
        return None

def upload_and_transcribe_groq_with_key(file_path, key, language=None):
    headers = {"Authorization": "Bearer %s" % key}
    data = {"model": GROQ_TRANSCRIBE_MODEL}
    if language:
        data["language"] = language
    mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    converted = None
    try:
        if not mime.startswith("audio"):
            tmp = convert_to_mp3(file_path)
            if tmp:
                converted = tmp
        else:
            tmp = convert_to_mp3(file_path)
            if tmp:
                converted = tmp
    except Exception:
        converted = None
    use_path = converted if converted else file_path
    use_mime = "audio/mpeg"
    files = {}
    try:
        with open(use_path, "rb") as f:
            files = {"file": ("audio.mp3", f, use_mime)}
            resp = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, data=data, files=files, timeout=REQUEST_TIMEOUT)
        try:
            resp.raise_for_status()
        except Exception as e:
            txt = ""
            try:
                txt = resp.text
            except:
                txt = str(e)
            raise RuntimeError("GROQ transcription error: %s" % txt)
        j = resp.json()
        if isinstance(j, dict):
            for k in ("text", "transcript", "result"):
                v = j.get(k)
                if isinstance(v, str) and v.strip():
                    return v
            if "data" in j and isinstance(j["data"], list) and j["data"]:
                first = j["data"][0]
                if isinstance(first, dict):
                    for kk in ("text", "transcript"):
                        vv = first.get(kk)
                        if isinstance(vv, str) and vv.strip():
                            return vv
            if "choices" in j and isinstance(j["choices"], list) and j["choices"]:
                c = j["choices"][0]
                if isinstance(c, dict):
                    if "message" in c and isinstance(c["message"], dict) and "content" in c["message"]:
                        return c["message"]["content"]
                    if "text" in c and isinstance(c["text"], str) and c["text"].strip():
                        return c["text"]
        raise RuntimeError("Unexpected transcription response from GROQ")
    finally:
        try:
            if converted and os.path.exists(converted) and converted != file_path:
                os.remove(converted)
        except:
            pass

def upload_and_transcribe_groq(file_path, language=None):
    if not groq_rotator.keys:
        raise RuntimeError("GROQ key(s) not configured")
    def perform(key):
        return upload_and_transcribe_groq_with_key(file_path, key, language=language)
    return execute_groq_action(perform)

def ask_groq_with_key(text, instruction, key):
    headers = {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}
    payload = {"model": GROQ_CHAT_MODEL, "messages": [{"role": "user", "content": "%s\n\n%s" % (instruction, text)}]}
    resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    j = resp.json()
    if isinstance(j, dict):
        if "choices" in j and isinstance(j["choices"], list) and j["choices"]:
            choice = j["choices"][0]
            if isinstance(choice, dict):
                if "message" in choice and isinstance(choice["message"], dict) and "content" in choice["message"]:
                    return choice["message"]["content"]
                if "text" in choice and isinstance(choice["text"], str):
                    return choice["text"]
    raise RuntimeError("Unexpected chat completion response from GROQ")

def ask_groq(text, instruction):
    if not groq_rotator.keys:
        raise RuntimeError("GROQ key(s) not configured")
    def perform(key):
        return ask_groq_with_key(text, instruction, key)
    return execute_groq_action(perform)

def build_action_keyboard(text_len):
    btns = []
    if text_len > 1000:
        btns.append([InlineKeyboardButton("Summarize", callback_data="summarize|")])
    return InlineKeyboardMarkup(btns)

def build_lang_keyboard(origin):
    btns, row = [], []
    for i, (lbl, code) in enumerate(LANGS, 1):
        row.append(InlineKeyboardButton(lbl, callback_data="lang|%s|%s|%s" % (code, lbl, origin)))
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
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Join", url="https://t.me/%s" % clean)]])
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
            "• Get Text for free \n\n"
            "• 💎 devloper by @orlaki"
        )
        kb = build_lang_keyboard("file")
        bot.reply_to(message, welcome_text, reply_markup=kb)

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
        bot.edit_message_text("you choosed: %s" % mode, call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass
    bot.answer_callback_query(call.id, "Mode set to: %s ☑️" % mode)

@bot.message_handler(commands=['lang'])
def lang_command(message):
    if ensure_joined(message):
        kb = build_lang_keyboard("file")
        bot.reply_to(message, "Select the language spoken in your audio or video:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith('lang|'))
def lang_cb(call):
    parts = call.data.split("|")
    if len(parts) != 4:
        try:
            bot.answer_callback_query(call.id, "Invalid language", show_alert=True)
        except:
            pass
        return
    _, code, lbl, origin = parts
    if origin != "file":
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        process_text_action(call, origin, "Translate to %s" % lbl, "Translate this text in to language %s. No extra text ONLY return the translated text." % lbl)
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
    try:
        bot.answer_callback_query(call.id, "Language set: %s ☑️" % lbl)
    except:
        pass

@bot.callback_query_handler(func=lambda c: c.data.startswith('translate_menu|') or c.data.startswith('summarize|'))
def action_cb(call):
    action, _ = call.data.split("|")
    if action == "translate_menu":
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=build_lang_keyboard(call.message.id))
    else:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        process_text_action(call, call.message.id, "Summarize", "Summarize this in original language.")

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
        res = ask_groq(text, prompt_instr)
        send_long_text(chat_id, res, data["origin"], call.from_user.id, log_action)
    except Exception:
        try:
            bot.send_message(chat_id, "error try again later")
        except:
            pass

def get_duration(path):
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        out = p.stdout.decode().strip()
        if not out:
            return None
        return float(out)
    except Exception:
        return None

def split_audio_to_chunks(input_path, chunk_seconds):
    tmpdir = tempfile.mkdtemp(prefix="chunks_", dir=DOWNLOADS_DIR)
    parts = []
    dur = get_duration(input_path)
    if not dur or dur <= 0:
        return parts
    start = 0
    idx = 0
    while start < dur:
        out_path = os.path.join(tmpdir, "chunk_%d.mp3" % idx)
        chunk_len = min(chunk_seconds, int(dur - start + 0.5))
        cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", input_path, "-t", str(chunk_len), "-ar", "16000", "-ac", "1", "-b:a", "64k", out_path]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                parts.append(out_path)
        except Exception:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except:
                pass
        idx += 1
        start += chunk_len
    if not parts:
        try:
            shutil.rmtree(tmpdir)
        except:
            pass
    return parts

def transcribe_with_chunking(file_path, language=None):
    dur = get_duration(file_path)
    if dur and dur > CHUNK_SECONDS:
        chunks = split_audio_to_chunks(file_path, CHUNK_SECONDS)
        texts = []
        try:
            for ch in chunks:
                txt = upload_and_transcribe_groq(ch, language=language)
                if txt:
                    texts.append(txt)
            combined = "\n".join(texts)
            return combined
        finally:
            try:
                if chunks:
                    parent = os.path.dirname(chunks[0])
                    shutil.rmtree(parent, ignore_errors=True)
            except:
                pass
    else:
        return upload_and_transcribe_groq(file_path, language=language)

def send_long_text(chat_id, text, reply_id, uid, action="Transcript"):
    mode = get_user_mode(uid)
    if len(text) > MAX_MESSAGE_CHUNK:
        if mode == "Split messages":
            sent = None
            for i in range(0, len(text), MAX_MESSAGE_CHUNK):
                sent = bot.send_message(chat_id, text[i:i+MAX_MESSAGE_CHUNK], reply_to_message_id=reply_id)
            return sent
        else:
            fname = os.path.join(DOWNLOADS_DIR, "%s.txt" % action)
            with open(fname, "w", encoding="utf-8") as f:
                f.write(text)
            sent = bot.send_document(chat_id, open(fname, 'rb'), caption="Open this file and copy the text inside 👍", reply_to_message_id=reply_id)
            os.remove(fname)
            return sent
    return bot.send_message(chat_id, text, reply_to_message_id=reply_id)

def keep_typing(chat_id, stop_event, interval=4):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, 'typing')
        except:
            pass
        stop_event.wait(interval)

update_queue = Queue()

@flask_app.route("/", methods=["GET"])
def index():
    return "Bot Running", 200

@flask_app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') and 'application/json' in request.headers.get('content-type'):
        data = request.get_json(force=True)
        try:
            update_queue.put_nowait(data)
        except Exception:
            update_queue.put(data)
        return '', 200
    abort(403)

@bot.message_handler(content_types=['voice', 'audio', 'video', 'document'])
def handle_media(message):
    if not ensure_joined(message):
        return
    media = message.voice or message.audio or message.video or message.document
    if not media:
        return
    if getattr(media, 'file_size', 0) > MAX_UPLOAD_SIZE:
        bot.reply_to(message, "Just send me a file less than %sMB 😎 or use @MediaToTextBot" % MAX_UPLOAD_MB)
        return
    bot.send_chat_action(message.chat.id, 'typing')
    file_path = os.path.join(DOWNLOADS_DIR, "temp_%s_%s" % (message.id, getattr(media, "file_unique_id", "x")))
    stop_event = threading.Event()
    t = threading.Thread(target=keep_typing, args=(message.chat.id, stop_event))
    t.daemon = True
    t.start()
    try:
        file_info = bot.get_file(media.file_id)
        downloaded = bot.download_file(file_info.file_path)
        with open(file_path, 'wb') as f:
            f.write(downloaded)
        lang = user_selected_lang.get(message.chat.id, "auto")
        lang_param = None if lang == "auto" else lang
        text = transcribe_with_chunking(file_path, language=lang_param)
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
    except Exception:
        try:
            bot.reply_to(message, "error try again later")
        except:
            pass
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except:
            pass
    finally:
        stop_event.set()
        try:
            t.join(1)
        except:
            pass
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except:
            pass

def worker_loop():
    while True:
        item = update_queue.get()
        try:
            if item is None:
                break
            try:
                u = Update.de_json(json.dumps(item))
                bot.process_new_updates([u])
            except Exception as e:
                logging.exception("Error processing update: %s", e)
        finally:
            update_queue.task_done()

if __name__ == "__main__":
    if WEBHOOK_URL:
        bot.remove_webhook()
        time.sleep(0.5)
        bot.set_webhook(url=WEBHOOK_URL)
        workers = []
        for i in range(MAX_WORKERS):
            w = threading.Thread(target=worker_loop)
            w.daemon = True
            w.start()
            workers.append(w)
        try:
            flask_app.run(host="0.0.0.0", port=PORT, threaded=True)
        finally:
            for _ in workers:
                update_queue.put(None)
            for w in workers:
                w.join(1)
    else:
        print("Webhook URL not set, exiting.")
