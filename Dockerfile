FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    INTEGRATION_MODE=mock \
    DATABASE_PATH=/app/data/commerce.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY influencer.html influencer.css influencer.js ./
COPY index.html styles.css app.js api.js ./

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
