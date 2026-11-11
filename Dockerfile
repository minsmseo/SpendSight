FROM python:3.11-slim

# tesseract-ocr: receipt OCR without an AI key. fonts-dejavu-core: font for the seeded demo receipts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
# Render sets PORT; the database is seeded on first start.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
