FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 5000
CMD gunicorn server:app --bind 0.0.0.0:$PORT --timeout 120 --workers 2
