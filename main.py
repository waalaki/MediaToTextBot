import os
import json
import threading
import requests
import logging
import time
import base64
import mimetypes
import io
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update
from groq import Groq

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
GROQ_KEY = os.environ.get("GROQ_KEY", "")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

client = Groq()

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
user_selected_lang = {}
pending_files = {}

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
flask_app = Flask(__name__)

def get_user_mode(uid):
    return user_mode.get(uid, "📄 Text File")

def transcribe_media_groq(file_url, mime_type, language_code):
    r = requests.get(file_url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    content = r.content
    file_obj = io.BytesIO(content)
    file_obj.name = "audio"
    try:
        transcription = client.audio.transcriptions.create(
            file=file_obj,
            model="whisper-large-v3-turbo",
            prompt="",
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
            language=language_code,
            temperature=0.0
        )
    except Exception as e:
        raise RuntimeError(f"Groq transcription error: {e}")
    text = None
    if hasattr(transcription, "text"):
        text = transcription.text
    elif isinstance(transcription, dict):
        text = transcription.get("text") or transcription.get("transcription") or json.dumps(transcription)
    else:
        try:
            text = str(transcription)
        except:
            text = ""
    return text or ""

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
            "• to transcribe using Groq AI\n\n"
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

def simple_sentences(text):
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]

def summarize_short(text):
    s = simple_sentences(text)
    if not s:
        return ""
    if len(s) <= 2:
        return " ".join(s)
    return " ".join(s[:2])

def summarize_detailed(text):
    s = simple_sentences(text)
    if not s:
        return ""
    return " ".join(s[:6])

def summarize_bulleted(text):
    s = simple_sentences(text)
    if not s:
        return ""
    bullets = ["- " + sent for sent in s[:10]]
    return "\n".join(bullets)

@bot.callback_query_handler(func=lambda c: c.data.startswith('lang|'))
def lang_cb(call):
    _, code, lbl, origin = call.data.split("|")
    if origin != "file":
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except:
            pass
        origin_msg = call.message
        target = lbl
        origin_id = call.message.reply_to_message.message_id if call.message.reply_to_message else call.message.message_id
        data = user_transcriptions.get(call.message.chat.id, {}).get(origin_id)
        if not data:
            bot.answer_callback_query(call.id, "Data not found (expired). Resend file.", show_alert=True)
            return
        text = data.get("text","")
        bot.send_chat_action(call.message.chat.id, 'typing')
        try:
            translated = translate_with_groq(text, code)
            send_long_text(call.message.chat.id, translated, data["origin"], call.from_user.id, "Translation")
        except Exception as e:
            bot.send_message(call.message.chat.id, f"Error: {e}")
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
        text = transcribe_media_groq(file_url, mime_type, code)
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
    if style == "Short":
        prompt = "short"
    elif style == "Detailed":
        prompt = "detailed"
    else:
        prompt = "bulleted"
    process_text_action(call, call.message.id, f"Summarize ({style})", prompt)

def translate_with_groq(text, target_lang_code):
    try:
        payload = f"Translate the following text to language code {target_lang_code}. Return only the translated text.\n\n{text}"
        resp = client.responses.create(model="groq-1", input=payload)
        if hasattr(resp, "output_text") and resp.output_text:
            return resp.output_text
        if isinstance(resp, dict):
            out = resp.get("output", [])
            if out:
                parts = []
                for item in out:
                    if isinstance(item, dict):
                        parts.append(item.get("content", [{}])[0].get("text",""))
                joined = "\n".join([p for p in parts if p])
                if joined:
                    return joined
        return text
    except Exception:
        return text

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
        if prompt_instr == "short":
            res = summarize_short(text)
        elif prompt_instr == "detailed":
            res = summarize_detailed(text)
        elif prompt_instr == "bulleted":
            res = summarize_bulleted(text)
        else:
            res = text
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
        bot.reply_to(message, f"File too large. Groq inline limit is {MAX_UPLOAD_MB}MB.")
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
        if not lang:
            pending_files[message.chat.id] = {"url": telegram_file_url, "mime": mime_type, "message": message}
            kb = build_lang_keyboard("file")
            bot.reply_to(message, "Select the language spoken in your audio or video:", reply_markup=kb)
            return
        text = transcribe_media_groq(telegram_file_url, mime_type, lang)
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
