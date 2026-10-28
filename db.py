"""Работа с SQLite: пользователи и обработанные партии."""
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL UNIQUE,
    chat_id          INTEGER NOT NULL,
    chess_username   TEXT    NOT NULL,
    registered_at    TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_chess_username ON users(chess_username);

-- Трекается по конкретной telegram-записи (user_id), а не по chess_username:
-- если один игрок привязан к нескольким людям, каждый получает партию независимо.
CREATE TABLE IF NOT EXISTS processed_games (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_url     TEXT    NOT NULL,
    end_time     INTEGER,
    processed_at TEXT    NOT NULL,
    UNIQUE (user_id, game_url)
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class User:
    id: int
    telegram_user_id: int
    chat_id: int
    chess_username: str
    registered_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "User":
        return cls(
            id=row["id"],
            telegram_user_id=row["telegram_user_id"],
            chat_id=row["chat_id"],
            chess_username=row["chess_username"],
            registered_at=datetime.fromisoformat(row["registered_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    def get_user(self, telegram_user_id: int) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
        return User.from_row(row) if row else None

    def save_user(self, telegram_user_id: int, chat_id: int, chess_username: str) -> tuple[User, str | None]:
        """Создаёт запись или меняет username у существующей.

        Возвращает (актуальная запись, предыдущий username или None, если запись новая).
        Если username не изменился, updated_at не трогаем.
        """
        now = utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chess_username FROM users WHERE telegram_user_id = ?", (telegram_user_id,)
            ).fetchone()
            previous = row["chess_username"] if row else None

            if row is None:
                conn.execute(
                    "INSERT INTO users (telegram_user_id, chat_id, chess_username, registered_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (telegram_user_id, chat_id, chess_username, now, now),
                )
            elif previous != chess_username:
                conn.execute(
                    "UPDATE users SET chat_id = ?, chess_username = ?, updated_at = ? WHERE telegram_user_id = ?",
                    (chat_id, chess_username, now, telegram_user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET chat_id = ? WHERE telegram_user_id = ?", (chat_id, telegram_user_id)
                )

            row = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (telegram_user_id,)).fetchone()
        return User.from_row(row), previous

    def list_users(self) -> list[User]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [User.from_row(r) for r in rows]

    def stats(self) -> dict[str, int]:
        day_ago = (utcnow() - timedelta(days=1)).isoformat()
        with self._connect() as conn:
            return dict(
                conn.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM users)                                     AS users,
                        (SELECT COUNT(DISTINCT chess_username) FROM users)               AS chess_accounts,
                        (SELECT COUNT(*) FROM processed_games)                           AS deliveries,
                        (SELECT COUNT(DISTINCT game_url) FROM processed_games)           AS unique_games,
                        (SELECT COUNT(*) FROM processed_games WHERE processed_at >= ?)   AS deliveries_24h
                    """,
                    (day_ago,),
                ).fetchone()
            )

    def processed_counts(self) -> dict[int, int]:
        """user_id -> сколько партий отправлено этой записи."""
        with self._connect() as conn:
            rows = conn.execute("SELECT user_id, COUNT(*) AS n FROM processed_games GROUP BY user_id").fetchall()
        return {r["user_id"]: r["n"] for r in rows}

    def get_processed_urls(self, user_id: int) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT game_url FROM processed_games WHERE user_id = ?", (user_id,)).fetchall()
        return {r["game_url"] for r in rows}

    def mark_processed(self, user_id: int, games: list[tuple[str, int]]) -> None:
        """games: [(game_url, end_time), ...]"""
        now = utcnow().isoformat()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO processed_games (user_id, game_url, end_time, processed_at) VALUES (?, ?, ?, ?)",
                [(user_id, url, end_time, now) for url, end_time in games],
            )
