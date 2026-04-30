import os
import asyncio
import base64
import httpx
import time
import json
import io
import struct
import random
from flask import Flask, request
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

# Importy Firebase
import firebase_admin
from firebase_admin import credentials, firestore

# --- KONFIGURACJA ---
API_KEY = os.environ.get("GEMINI_API_KEY", "") 
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
FIREBASE_JSON = os.environ.get("FIREBASE_CONFIG", "")
APP_ID = os.environ.get("APP_ID", "karyna_bot_gcp")
ALLOWED_TOPIC_ID = "60061"

MODEL_TEXT = "gemini-2.5-flash"
MODEL_TTS = "gemini-2.5-flash-preview-tts"

db = None
if FIREBASE_JSON:
    try:
        cred_dict = json.loads(FIREBASE_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(">>> Połączono z Firebase!")
    except Exception as e:
        print(f">>> Błąd Firebase: {e}")

def pcm_to_wav(pcm_data, sample_rate=24000):
    wav_buf = io.BytesIO()
    n_channels = 1
    sample_width = 2
    wav_buf.write(b'RIFF')
    wav_buf.write(struct.pack('<I', 36 + len(pcm_data)))
    wav_buf.write(b'WAVE')
    wav_buf.write(b'fmt ')
    wav_buf.write(struct.pack('<I', 16))
    wav_buf.write(struct.pack('<H', 1))
    wav_buf.write(struct.pack('<H', n_channels))
    wav_buf.write(struct.pack('<I', sample_rate))
    wav_buf.write(struct.pack('<I', sample_rate * n_channels * sample_width))
    wav_buf.write(struct.pack('<H', n_channels * sample_width))
    wav_buf.write(struct.pack('<H', sample_width * 8))
    wav_buf.write(b'data')
    wav_buf.write(struct.pack('<I', len(pcm_data)))
    wav_buf.write(pcm_data)
    wav_buf.seek(0)
    return wav_buf

async def get_chat_history(limit=8):
    if not db: return ""
    try:
        logs_ref = db.collection("artifacts").document(APP_ID).collection("public").document("data").collection("chat_logs")
        docs = logs_ref.limit(50).get()
        history = [d.to_dict() for d in docs]
        history.sort(key=lambda x: x.get('timestamp') or 0)
        recent = history[-limit:]
        return "\n".join([f"{m.get('user', 'Anon')}: {m.get('text', '')}" for m in recent])
    except: return ""

async def call_gemini(url, payload):
    async with httpx.AsyncClient() as client:
        try:
            res = await client.post(url, json=payload, timeout=60.0)
            if res.status_code == 200:
                return res.json()
            print(f">>> Błąd API {res.status_code}: {res.text}")
            return None
        except Exception as e:
            print(f">>> Wyjątek API: {e}")
            return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return

    text = msg.text or msg.caption or ""
    current_topic_id = str(msg.message_thread_id) if msg.message_thread_id else "None"

    # --- ANTY-SPAM ---
    if current_topic_id in ["None", "0"]:
        if "potencjalny dochód" in text.lower():
            try:
                await msg.delete()
                await context.bot.send_message(chat_id=msg.chat_id, text="Wyjazd z tym spamem, mordo! 🖕", message_thread_id=msg.message_thread_id)
                return
            except: pass

    is_reply = msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id
    should_respond = "karyna" in text.lower() or is_reply or msg.chat.type == "private"

    if should_respond:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE, message_thread_id=msg.message_thread_id)

        history = await get_chat_history(5)
        system_prompt = (
            "Jesteś Karyną, napaloną dresiarą z osiedla. Mówisz slangiem, jesteś flirciarska i agresywna. "
            f"Historia: {history}"
        )

        # KROK 1: Najpierw sam TEKST (najbardziej stabilne)
        url_text = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_TEXT}:generateContent?key={API_KEY}"
        payload_text = {
            "contents": [{"parts": [{"text": text}]}],
            "systemInstruction": {"parts": [{"text": system_prompt}]}
        }
        
        text_data = await call_gemini(url_text, payload_text)
        resp_text = text_data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', "Coś mi styki upaliło, sorki.") if text_data else "Sorki mordo, Google mnie blokuje (Error 429/400)."

        # KROK 2: Próba AUDIO dla wygenerowanego tekstu
        url_tts = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_TTS}:generateContent?key={API_KEY}"
        payload_tts = {
            "contents": [{"parts": [{"text": resp_text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": "Kore"}
                    }
                }
            }
        }

        audio_data = await call_gemini(url_tts, payload_tts)
        
        if audio_data:
            parts = audio_data.get('candidates', [{}])[0].get('content', {}).get('parts', [])
            audio_part = next((p for p in parts if "inlineData" in p), None)
            if audio_part:
                pcm_bytes = base64.b64decode(audio_part["inlineData"]["data"])
                wav_file = pcm_to_wav(pcm_bytes)
                wav_file.name = "karyna_voice.wav"
                await msg.reply_voice(voice=wav_file, caption=resp_text, message_thread_id=msg.message_thread_id)
                return

        # Jeśli Audio padło (429), wysyłamy tylko tekst
        await msg.reply_text(resp_text, message_thread_id=msg.message_thread_id)

application = ApplicationBuilder().token(TG_TOKEN).build()
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, handle_message))

app = Flask(__name__)

@app.route("/", methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    asyncio.run(application.initialize())
    update = Update.de_json(data, application.bot)
    asyncio.run(application.process_update(update))
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
