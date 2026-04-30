FROM python:3.10-slim

# Ustawienia środowiska
ENV PYTHONUNBUFFERED True
ENV APP_HOME /app
WORKDIR $APP_HOME

# Instalacja zależności
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopiowanie kodu
COPY . ./

# Cloud Run wymaga gunicorna do stabilnego działania
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 bot:app