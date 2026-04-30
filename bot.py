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
# Pobierane z Environment Variables w Google Cloud Console
API_KEY = os.environ.get("GEMINI_API_KEY", "") 
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# FIREBASE_CONFIG powinien zawierać cały JSON pobrany z Google Firebase Console (Service Account)
FIREBASE_JSON = os.environ.get("FIREBASE_CONFIG", "")
# ID aplikacji używane do struktury folderów w bazie danych
APP_ID = os.environ.get("APP_ID", "karyna_bot_gcp")

# --- INICJALIZACJA FIREBASE (RULE 3) ---
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
else:
    print(">>> Brak FIREBASE_CONFIG! Bot działa bez pamięci długotrwałej.")

MODEL_NAME = "gemini-2.5-flash-preview-09-2025"
# Tutaj wpisz ID swojej podgrupy (topic_id), jeśli chcesz ograniczyć reakcje bota
ONLY_TOPIC_ID = None 

# --- LOGOWANIE ---
def log(msg):
    # Logi widoczne w konsoli Google Cloud Logging
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# --- OPERACJE NA BAZIE DANYCH (RULE 1 & 2) ---
async def save_message_to_db(user_name, text, topic_id):
    """Zapisuje wiadomość do bazy Firestore zgodnie z wymaganą strukturą ścieżek."""
    if not db: return
    try:
        # Ścieżka: /artifacts/{appId}/public/data/{collectionName} (Zasada RULE 1)
        doc_ref = db.collection("artifacts").document(APP_ID).collection("public").document("data").collection("chat_logs").document()
        doc_ref.set({
            "user": user_name,
            "text": text,
            "topic_id": topic_id,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    except Exception as e:
        log(f"Błąd zapisu do Firestore: {e}")

async def get_chat_history(limit=25):
    """Pobiera ostatnie wiadomości z bazy Firestore (RULE 2 - filtrowanie w pamięci)."""
    if not db: return ""
    try:
        # Pobieramy całą kolekcję logów dla danego bota
        logs_ref = db.collection("artifacts").document(APP_ID).collection("public").document("data").collection("chat_logs")
        docs = logs_ref.stream()
        
        history = []
        for d in docs:
            history.append(d.to_dict())
            
        # Sortowanie w pamięci RAM zamiast w zapytaniu (RULE 2 - unikanie indeksów)
        history.sort(key=lambda x: x.get('timestamp') or 0)
        
        # Formatowanie ostatnich wiadomości dla AI
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

    # Każda wiadomość leci do bazy, żeby Karyna "wiedziała o czym gadacie"
    if text:
        await save_message_to_db(user_name, text, topic_id)

    # Komenda serwisowa do wyciągania ID podgrupy
    if "karyna jakie to id" in text.lower():
        await msg.reply_text(f"Mordo, ID tej podgrupy to: `{topic_id}`", parse_mode='Markdown')
        return

    # Jeśli ustawiliśmy blokadę na konkretny temat, bot ignoruje resztę
    if ONLY_TOPIC_ID is not None and topic_id != ONLY_TOPIC_ID:
        return

    # Karyna reaguje, gdy ktoś wywoła jej imię
    if "karyna" in text.lower():
        log(f"Reakcja Karyny dla {user_name} w podgrupie {topic_id}")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        # Pobieramy kontekst historyczny z Firebase
        history_context = await get_chat_history(30)

        system_prompt = (
            "Jesteś Karyną. Dziewczyna z polskiego osiedla, pyskata, ale lojalna ziomalka. "
            "Mówisz szorstko, potocznie, po polsku. Używasz slangu (mordo, mordeczko, nie sraj żarem, co jest grane). "
            "Oto kontekst waszej rozmowy z bazy danych:\n"
            f"{history_context}\n\n"
            "ZASADA: Jeśli nie znasz odpowiedzi na podstawie historii lub faktów, nie zmyślaj głupot. "
            "Bądź szczera – jeśli czegoś nie wiesz, powiedz np. 'nie wiem kurwa', 'skąd mam to wiedzieć?'."
        )

        # Obsługa obrazków (Gemini Image Understanding)
        image_part = None
        if msg.photo:
            try:
                photo_file = await msg.photo[-1].get_file()
                image_bytes = await photo_file.download_as_bytearray()
                image_part = {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(image_bytes).decode('utf-8')}}
            except: pass

        # Zapytanie do API Gemini (z obsługą retries i brakiem streamingu)
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
                # Wywołanie modelu gemini-2.5-flash-preview-09-2025
                res = await client.post(url, json=payload, timeout=30.0)
                if res.status_code == 200:
                    data = res.json()
                    ans = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', "")
                    if ans:
                        await msg.reply_text(ans)
                else:
                    log(f"Błąd Gemini API: {res.status_code} - {res.text}")
            except Exception as e:
                log(f"Błąd komunikacji z AI: {e}")

# --- SERWER FLASK (Health Check) ---
# Wymagany przez Google Cloud Run, aby wiedzieć, czy kontener żyje
app = Flask(__name__)
@app.route("/")
def health_check():
    return "Karyna AI Status: Active and Toxic", 200

def main():
    # Google Cloud Run przypisuje port automatycznie przez zmienną środowiskową
    port = int(os.environ.get("PORT", 8080))
    log(f"Uruchamiam serwer Flask na porcie {port}")
    
    # Flask leci w osobnym wątku, żeby nie blokował bota Telegrama
    Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True).start()

    # Inicjalizacja bota Telegram
    application = ApplicationBuilder().token(TG_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, handle_message))
    
    log("Karyna wystartowała i czeka na dym!")
    application.run_polling()

if __name__ == "__main__":
    main()