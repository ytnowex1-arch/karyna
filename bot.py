import os
import asyncio
import base64
import httpx
import time
import json
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

# --- KONFIGURACJA ZMIENNYCH ---
API_KEY = os.environ.get("GEMINI_API_KEY", "") 
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
FIREBASE_JSON = os.environ.get("FIREBASE_CONFIG", "")
APP_ID = os.environ.get("APP_ID", "karyna_bot_gcp")

# ID DOZWOLONEJ PODGRUPY (TOPIC ID)
ALLOWED_TOPIC_ID = 60061

# --- INICJALIZACJA FIREBASE ---
db = None
if FIREBASE_JSON:
    try:
        cred_dict = json.loads(FIREBASE_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(">>> Połączono z Firebase pomyślnie!")
    except Exception as e:
        print(f">>> Błąd połączenia z Firebase: {e}")

MODEL_NAME = "gemini-2.5-flash-preview-09-2025"

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# --- OPERACJE NA BAZIE DANYCH ---
async def save_message_to_db(user_name, text, topic_id):
    if not db: return
    try:
        doc_ref = db.collection("artifacts").document(APP_ID).collection("public").document("data").collection("chat_logs").document()
        doc_ref.set({
            "user": user_name,
            "text": text,
            "topic_id": topic_id,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        log(f"Błąd zapisu do Firestore: {e}")

async def get_chat_history(limit=20):
    if not db: return ""
    try:
        logs_ref = db.collection("artifacts").document(APP_ID).collection("public").document("data").collection("chat_logs")
        docs = logs_ref.limit(50).get()
        
        history = []
        for d in docs:
            history.append(d.to_dict())
            
        history.sort(key=lambda x: x.get('timestamp') or 0)
        recent = history[-limit:]
        return "\n".join([f"{m.get('user', 'Anonim')}: {m.get('text', '')}" for m in recent])
    except Exception as e:
        log(f"Błąd pobierania historii: {e}")
        return ""

# --- OBSŁUGA TELEGRAMA ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg: return

    text = msg.text or msg.caption or ""
    user_name = msg.from_user.full_name or "Ziomek"
    topic_id = msg.message_thread_id 

    # BLOKADA: Karyna reaguje tylko w wyznaczonym temacie (lub w czacie prywatnym)
    if msg.chat.type != "private" and topic_id != ALLOWED_TOPIC_ID:
        return

    # Diagnostyka ID tematu
    if "karyna jakie to id" in text.lower():
        await msg.reply_text(f"Mordo, ID tej podgrupy to: `{topic_id}`", parse_mode='Markdown', message_thread_id=topic_id)
        return

    if text:
        await save_message_to_db(user_name, text, topic_id)

    is_reply_to_bot = msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id
    should_respond = "karyna" in text.lower() or is_reply_to_bot or msg.chat.type == "private"

    if should_respond:
        log(f"Karyna generuje odpowiedź dla {user_name} (Wątek: {topic_id})")
        try:
            await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING, message_thread_id=topic_id)
        except: pass

        history_context = await get_chat_history(25)
        system_prompt = (
            "Jesteś Karyną. Dziewczyna z polskiego osiedla, pyskata, ale lojalna ziomalka. "
            "Mówisz szorstko, potocznie, po polsku. Używasz slangu (mordo, ziom, lipa, ogarnij się). "
            "Oto historia rozmowy:\n"
            f"{history_context}\n\n"
            "Odpowiedz krótko i w swoim stylu."
        )

        image_part = None
        if msg.photo:
            try:
                photo_file = await msg.photo[-1].get_file()
                image_bytes = await photo_file.download_as_bytearray()
                image_part = {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(image_bytes).decode('utf-8')}}
            except: pass

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={API_KEY}"
        
        contents_parts = [{"text": text}]
        if image_part:
            contents_parts.append(image_part)

        payload = {
            "contents": [{"role": "user", "parts": contents_parts}],
            "systemInstruction": {"parts": [{"text": system_prompt}]}
        }

        async with httpx.AsyncClient() as client:
            try:
                res = await client.post(url, json=payload, timeout=30.0)
                if res.status_code == 200:
                    data = res.json()
                    ans = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', "")
                    if ans:
                        await msg.reply_text(ans, message_thread_id=topic_id)
                else:
                    log(f"Błąd Gemini: {res.status_code}")
            except Exception as e:
                log(f"Błąd komunikacji: {e}")

# --- KONFIGURACJA SERWERA ---
application = ApplicationBuilder().token(TG_TOKEN).build()
application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, handle_message))

app = Flask(__name__)
initialized = False

async def boot_bot():
    global initialized
    if not initialized:
        await application.initialize()
        initialized = True

@app.route("/", methods=['GET', 'POST'])
def webhook():
    if request.method == 'POST':
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            data = request.get_json(force=True)
            loop.run_until_complete(boot_bot())
            update = Update.de_json(data, application.bot)
            loop.run_until_complete(application.process_update(update))
        finally:
            loop.close()
        return "OK", 200
    return "Karyna AI is ready and watching you.", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
