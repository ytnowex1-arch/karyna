import os
import asyncio
import base64
import httpx
import time
import json
from threading import Thread
from flask import Flask
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

# Ustaw na None, aby bot działał we wszystkich podgrupach (topicach)
ONLY_TOPIC_ID = None 

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
        # Ścieżka zgodna z RULE 1
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
        # Proste zapytanie (RULE 2)
        docs = logs_ref.limit(50).get()
        
        history = []
        for d in docs:
            history.append(d.to_dict())
            
        # Sortowanie w pamięci RAM
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
    topic_id = msg.message_thread_id # To jest ID podgrupy

    # 1. Funkcja diagnostyczna - sprawdzenie ID
    if "karyna jakie to id" in text.lower():
        await msg.reply_text(f"Mordo, ID tej podgrupy (tematu) to: `{topic_id}`", parse_mode='Markdown')
        return

    # 2. Logowanie wiadomości do bazy (zawsze, jeśli jest tekst)
    if text:
        await save_message_to_db(user_name, text, topic_id)

    # 3. Sprawdzenie, czy bot ma reagować (czy słowo 'karyna' jest w tekście lub czy to reply do bota)
    is_reply_to_bot = msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id
    
    should_respond = "karyna" in text.lower() or is_reply_to_bot or msg.chat.type == "private"

    if should_respond:
        # Filtr podgrupy (jeśli ustawiony)
        if ONLY_TOPIC_ID is not None and topic_id != ONLY_TOPIC_ID:
            log(f"Ignoruję wiadomość z podgrupy {topic_id}")
            return

        log(f"Karyna generuje odpowiedź dla {user_name} w podgrupie {topic_id}")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        history_context = await get_chat_history(25)

        system_prompt = (
            "Jesteś Karyną. Dziewczyna z polskiego osiedla, pyskata, ale lojalna ziomalka. "
            "Mówisz szorstko, potocznie, po polsku. Używasz slangu (mordo, ziom, lipa, ogarnij się). "
            "Jesteś na grupie z ziomkami. Oto co pisali wcześniej:\n"
            f"{history_context}\n\n"
            "Odpowiedz krótko i konkretnie na ostatnią wiadomość."
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
                        await msg.reply_text(ans)
                else:
                    log(f"Błąd Gemini API: {res.status_code}")
            except Exception as e:
                log(f"Błąd komunikacji: {e}")

# --- SERWER FLASK ---
app = Flask(__name__)
@app.route("/")
def health_check():
    return "OK", 200

def main():
    port = int(os.environ.get("PORT", 8080))
    Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()

    application = ApplicationBuilder().token(TG_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.CAPTION, handle_message))
    
    log("Karyna gotowa do akcji na podgrupach!")
    application.run_polling()

if __name__ == "__main__":
    main()
