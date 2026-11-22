FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHESS_DB=/data/chess_bot.db \
    WEBAPP_PORT=10000 \
    BACKUP_ENABLED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chess_bot ./chess_bot
COPY tools ./tools

# локальный каталог копий; на Render база живёт в Postgres, а копии уходят в Drive
RUN mkdir -p /data /app/backups

EXPOSE 10000

CMD ["python", "-m", "chess_bot.web_server", "--host", "0.0.0.0", "--port", "10000"]