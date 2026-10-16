FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000
CMD gunicorn web_app:app --bind 0.0.0.0:$PORT --workers 1 --preload --timeout 120
