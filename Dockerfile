FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    EVALAI_DATA_DIR=/var/data \
    EVALAI_DATABASE_URL=sqlite:////var/data/evalai.sqlite3 \
    EVALAI_UPLOAD_DIR=/var/data/uploads \
    EVALAI_OCR_PROVIDER=demo \
    EVALAI_EVALUATION_PROVIDER=demo \
    EVALAI_EMAIL_PROVIDER=simulation \
    EVALAI_COOKIE_SECURE=false

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY backend/app /app/backend/app
COPY frontend /app/frontend
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin evalai \
    && mkdir -p /var/data/uploads \
    && chmod 755 /app/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
