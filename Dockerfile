FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV BACKGROUND_WORKERS=1
ENV DEFAULT_MAX_PAGES=15
ENV PDF_RENDER_SCALE=2.2
ENV FUZZY_MATCH_THRESHOLD=92
ENV MAX_UPLOAD_MB=1500
EXPOSE 10000
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--timeout", "1200", "--workers", "1", "app:app"]
