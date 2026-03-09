FROM python:3.11-slim

# Installer Chrome
RUN apt-get update && apt-get install -y \
    wget gnupg curl unzip \
    chromium chromium-driver \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Variables pour Selenium
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 5000
CMD gunicorn server:app --bind 0.0.0.0:$PORT --timeout 120 --workers 1
