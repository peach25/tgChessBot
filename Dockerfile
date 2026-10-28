FROM python:3.12-slim-bookworm

# Stockfish из репозитория Debian ставится в /usr/games/stockfish; sqlite3 — для ручного обслуживания БД
RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish sqlite3 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STOCKFISH_PATH=/usr/games/stockfish \
    DB_PATH=/app/data/bot.db \
    LOG_FILE=/app/logs/bot.log

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Непривилегированный пользователь; каталоги данных создаются заранее, чтобы
# именованные тома docker при первом подключении унаследовали владельца
RUN useradd --system --uid 10001 --create-home chessbot \
    && mkdir -p /app/data /app/logs \
    && chown -R chessbot:chessbot /app/data /app/logs
USER chessbot

VOLUME ["/app/data", "/app/logs"]

CMD ["python", "bot.py"]
