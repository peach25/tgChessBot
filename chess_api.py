"""Клиент публичного API Chess.com (https://www.chess.com/news/view/published-data-api)."""
import logging
import re
from dataclasses import dataclass

import httpx

import config

log = logging.getLogger(__name__)

API_BASE = "https://api.chess.com/pub"
# Chess.com: 3–25 символов, латиница, цифры, '_' и '-'
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,25}$")


class ChessComError(Exception):
    """Chess.com недоступен или ответил что-то неожиданное."""


class PlayerNotFound(ChessComError):
    """Такого игрока на Chess.com нет."""


def normalize_username(raw: str) -> str | None:
    """Чистит ввод пользователя; None, если это точно не username Chess.com."""
    username = raw.strip().lstrip("@")
    # Разрешаем вставить ссылку на профиль: https://www.chess.com/member/hikaru
    match = re.search(r"chess\.com/member/([^/?#\s]+)", username)
    if match:
        username = match.group(1)
    if not USERNAME_RE.fullmatch(username):
        return None
    # API Chess.com регистронезависим, храним в нижнем регистре
    return username.lower()


async def _get_json(path: str) -> dict:
    url = f"{API_BASE}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=config.HTTP_TIMEOUT, headers={"User-Agent": config.CHESSCOM_USER_AGENT}
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise ChessComError(f"Сетевая ошибка при запросе {url}: {e!r}") from e

    if resp.status_code in (404, 410):
        # Chess.com иногда отвечает 404 "An internal error has occurred" на существующего игрока
        # (обычно архив текущего месяца у очень активных игроков) — это временный сбой, а не «нет игрока».
        message = _error_message(resp)
        if "not found" in message.lower() or resp.status_code == 410:
            raise PlayerNotFound(url)
        raise ChessComError(f"{url} вернул HTTP {resp.status_code}: {message[:200]}")
    if resp.status_code != 200:
        raise ChessComError(f"{url} вернул HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise ChessComError(f"{url} вернул не JSON") from e


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message", ""))
    except (ValueError, AttributeError):
        return resp.text


async def get_player(username: str) -> dict:
    """Профиль игрока. PlayerNotFound, если игрока нет."""
    return await _get_json(f"/player/{username}")


DRAW_RESULTS = {"agreed", "repetition", "stalemate", "insufficient", "50move", "timevsinsufficient"}


@dataclass(frozen=True)
class Side:
    username: str
    rating: int | None
    result: str  # код Chess.com: win, resigned, timeout, agreed, ...


@dataclass(frozen=True)
class Game:
    url: str
    end_time: int
    pgn: str
    time_class: str
    time_control: str
    rules: str
    rated: bool
    white: Side
    black: Side

    @classmethod
    def from_json(cls, data: dict) -> "Game":
        def side(d: dict) -> Side:
            return Side(username=d["username"], rating=d.get("rating"), result=d.get("result", ""))

        return cls(
            url=data["url"],
            end_time=int(data["end_time"]),
            pgn=data.get("pgn", ""),
            time_class=data.get("time_class", ""),
            time_control=data.get("time_control", ""),
            rules=data.get("rules", "chess"),
            rated=bool(data.get("rated")),
            white=side(data["white"]),
            black=side(data["black"]),
        )

    def perspective(self, username: str) -> tuple[str, Side, Side]:
        """(цвет игрока, его сторона, соперник) для данного username."""
        if self.white.username.lower() == username.lower():
            return "white", self.white, self.black
        return "black", self.black, self.white

    def outcome(self, username: str) -> str:
        """'win' / 'draw' / 'loss' с точки зрения username."""
        _, me, _ = self.perspective(username)
        if me.result == "win":
            return "win"
        if me.result in DRAW_RESULTS:
            return "draw"
        return "loss"


async def get_monthly_games(username: str, year: int, month: int) -> list[Game]:
    """Партии игрока за месяц (по возрастанию end_time). Битые записи пропускаются."""
    data = await _get_json(f"/player/{username}/games/{year}/{month:02d}")
    games = []
    for raw in data.get("games", []):
        try:
            games.append(Game.from_json(raw))
        except (KeyError, TypeError, ValueError) as e:
            log.warning("Пропускаю некорректную партию %s: %r", raw.get("url"), e)
    games.sort(key=lambda g: g.end_time)
    return games
