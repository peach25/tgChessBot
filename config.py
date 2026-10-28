"""Настройки бота: читаются из .env (или переменных окружения), ничего не хардкодится."""
import logging
import os
import shutil
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Не задана переменная {name} (см. .env.example)")
    return value


def _require_int(name: str) -> int:
    value = _require(name)
    try:
        return int(value)
    except ValueError:
        sys.exit(f"Переменная {name} должна быть числом, получено: {value!r}")


TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
ADMIN_TELEGRAM_ID = _require_int("ADMIN_TELEGRAM_ID")

DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "data" / "bot.db"))
LOG_FILE = Path(os.getenv("LOG_FILE", BASE_DIR / "logs" / "bot.log"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Chess.com просит указывать контакт в User-Agent для публичного API
CHESSCOM_USER_AGENT = os.getenv("CHESSCOM_USER_AGENT", "chess-tgbot/1.0")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))

# Часовой пояс для времени в сообщениях (в БД время хранится в UTC)
try:
    TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "").strip() or "Europe/Moscow")
except (ZoneInfoNotFoundError, ValueError):
    sys.exit(f"Неизвестный часовой пояс TIMEZONE={os.getenv('TIMEZONE')!r}, пример: Europe/Moscow")


def format_local(dt: datetime, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Время в часовом поясе TIMEZONE с подписью пояса, например «28.10.2026 13:05 MSK»."""
    local = dt.astimezone(TIMEZONE)
    return f"{local.strftime(fmt)} {local.strftime('%Z')}"

# Фоновая проверка новых партий
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
# Сколько партий максимум отправлять одному пользователю за одну проверку (остальные — в следующий раз)
MAX_GAMES_PER_CHECK = int(os.getenv("MAX_GAMES_PER_CHECK", "5"))

# Stockfish
STOCKFISH_PATH = os.getenv("STOCKFISH_PATH") or shutil.which("stockfish") or "/usr/games/stockfish"
STOCKFISH_DEPTH = int(os.getenv("STOCKFISH_DEPTH", "14"))
STOCKFISH_THREADS = int(os.getenv("STOCKFISH_THREADS", "1"))
STOCKFISH_HASH_MB = int(os.getenv("STOCKFISH_HASH_MB", "64"))
# Предохранители: лимит на одну позицию, на всю партию и на запуск движка (секунды)
STOCKFISH_MOVE_TIME_LIMIT = float(os.getenv("STOCKFISH_MOVE_TIME_LIMIT", "3"))
STOCKFISH_GAME_TIMEOUT = float(os.getenv("STOCKFISH_GAME_TIMEOUT", "300"))
STOCKFISH_START_TIMEOUT = float(os.getenv("STOCKFISH_START_TIMEOUT", "15"))


def setup_logging() -> None:
    """Лог одновременно в файл (с ротацией) и в stdout (его подхватывает journald)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.handlers[:] = [file_handler, stream_handler]

    # httpx на INFO пишет каждый запрос вместе с URL, а в URL Telegram API лежит токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
