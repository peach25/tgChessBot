"""Фоновая проверка новых партий для всех зарегистрированных пользователей.

Запускается из bot.py через JobQueue раз в CHECK_INTERVAL_SECONDS в том же процессе.

Логика:
- архив Chess.com скачивается один раз на chess_username за проверку (даже если за игроком
  следят несколько telegram-пользователей);
- «новые» партии считаются отдельно для каждой telegram-записи: закончились после её
  updated_at (регистрации / последней смены username) и ещё не отмечены в processed_games
  для этой записи;
- партия анализируется Stockfish один раз за проверку, даже если её получат несколько подписчиков;
- на каждую партию — отдельное сообщение; партия отмечается обработанной только после успешной
  отправки в chat_id этой записи. Если анализ не удался, отчёт уходит без него.
"""
import html
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

import chess
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

import chess_api
import config
from analyzer import AnalysisError, Analyzer, GameAnalysis, SideSummary
from db import Database, User

log = logging.getLogger(__name__)

OUTCOME_LABELS = {"win": "✅ Победа", "draw": "🤝 Ничья", "loss": "❌ Поражение"}


def _months_to_fetch(since: datetime, now: datetime) -> list[tuple[int, int]]:
    """Текущий месяц, плюс предыдущий, если отслеживание началось раньше начала текущего.
    Дальше предыдущего месяца не смотрим — чтобы не прислать пачку старых партий."""
    current = (now.year, now.month)
    previous = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return [previous, current] if (since.year, since.month) < current else [current]


async def _fetch_games(username: str, since: datetime, now: datetime) -> list[chess_api.Game]:
    games: list[chess_api.Game] = []
    for year, month in _months_to_fetch(since, now):
        games.extend(await chess_api.get_monthly_games(username, year, month))
    return games


def format_time_control(tc: str) -> str:
    if "/" in tc:  # daily: "1/86400" — секунд на ход
        try:
            return f"{int(tc.split('/')[1]) // 86400} дн/ход"
        except ValueError:
            return tc
    base, _, inc = tc.partition("+")
    try:
        base_s = int(base)
    except ValueError:
        return tc
    base_str = f"{base_s // 60}" if base_s >= 60 and base_s % 60 == 0 else f"{base_s}с"
    return f"{base_str}+{inc or 0}"


CATEGORY_MARKS = {"blunder": "??", "mistake": "?", "inaccuracy": "?!"}


def _format_side(name: str, s: SideSummary) -> str:
    return (
        f"<b>{html.escape(name)}</b>: зевков {s.blunders} · ошибок {s.mistakes} · "
        f"неточностей {s.inaccuracies} · ср. потеря {s.acpl} cp"
    )


def format_report(username: str, g: chess_api.Game, analysis: GameAnalysis | None, error: str | None) -> str:
    color, me, opp = g.perspective(username)
    outcome = g.outcome(username)
    reason = opp.result if outcome == "win" else me.result
    ended = datetime.fromtimestamp(g.end_time, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    variant = "" if g.rules == "chess" else f" · {html.escape(g.rules)}"
    rated = "рейтинговая" if g.rated else "без рейтинга"

    lines = [
        f"♟ <b>{OUTCOME_LABELS[outcome]}</b> ({html.escape(reason)})",
        f"{html.escape(me.username)} ({me.rating}) vs {html.escape(opp.username)} ({opp.rating})",
        f"{html.escape(g.time_class)} {format_time_control(g.time_control)}{variant} · "
        f"{'белыми' if color == 'white' else 'чёрными'} · {rated}",
        f'{ended} · <a href="{html.escape(g.url)}">открыть партию</a>',
        "",
    ]

    if analysis is None:
        lines.append(f"⚠️ Анализ недоступен: {html.escape(error or 'неизвестная ошибка')}")
        return "\n".join(lines)

    my_color = chess.WHITE if color == "white" else chess.BLACK
    mine = analysis.summary(my_color)
    theirs = analysis.summary(not my_color)
    lines += [
        f"🔍 Stockfish, глубина {analysis.depth} (оценки — с точки зрения белых)",
        _format_side(me.username, mine),
        _format_side(opp.username, theirs),
        "",
    ]
    if mine.worst:
        lines.append("Худшие ходы:")
        for m in mine.worst:
            mark = CATEGORY_MARKS.get(m.category, "")
            better = f", лучше <b>{html.escape(m.best_san)}</b>" if m.best_san else ""
            lines.append(
                f"• {m.label} <b>{html.escape(m.san)}</b>{mark} — потеря {m.cp_loss} cp "
                f"({m.before.text} → {m.after.text}){better}"
            )
    else:
        lines.append("Ни одного хода с потерей — чистая партия 👍")
    return "\n".join(lines)


class AnalysisCache:
    """Анализ на одну проверку: партия, которая нужна нескольким подписчикам, анализируется один раз."""

    def __init__(self, analyzer: Analyzer):
        self.analyzer = analyzer
        self._results: dict[str, tuple[GameAnalysis | None, str | None]] = {}

    async def get(self, game: chess_api.Game) -> tuple[GameAnalysis | None, str | None]:
        if game.url not in self._results:
            self._results[game.url] = await self._analyse(game)
        return self._results[game.url]

    async def _analyse(self, game: chess_api.Game) -> tuple[GameAnalysis | None, str | None]:
        if game.rules != "chess":
            return None, f"вариант «{game.rules}» не поддерживается"
        try:
            started = time.monotonic()
            result = await self.analyzer.analyse_pgn(game.pgn)
            log.info(
                "Проанализирована %s: %d полуходов за %.1f с", game.url, len(result.moves), time.monotonic() - started
            )
            return result, None
        except AnalysisError as e:
            log.error("Анализ %s не удался: %s", game.url, e)
            return None, "Stockfish не смог проанализировать партию"
        except Exception:
            log.exception("Неожиданная ошибка анализа %s", game.url)
            return None, "внутренняя ошибка анализа"


async def _process_user(bot: Bot, db: Database, user: User, games: list[chess_api.Game], cache: AnalysisCache) -> int:
    """Отправляет пользователю отчёты по его новым партиям. Возвращает число отправленных."""
    since = user.updated_at.timestamp()
    done = db.get_processed_urls(user.id)
    new = [g for g in games if g.end_time > since and g.url not in done]
    if not new:
        return 0

    batch = new[: config.MAX_GAMES_PER_CHECK]
    sent = 0
    for i, game in enumerate(batch):
        analysis, error = await cache.get(game)
        text = format_report(user.chess_username, game, analysis, error)
        if i == len(batch) - 1 and len(new) > len(batch):
            text += f"\n\n…ещё {len(new) - len(batch)} партий пришлю при следующей проверке."
        try:
            await bot.send_message(user.chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Forbidden:
            # Пользователь заблокировал бота — партии не отмечаем, попробуем снова в следующий раз
            log.warning("tg=%s заблокировал бота, пропускаю", user.telegram_user_id)
            break
        except TelegramError as e:
            log.error("Не удалось отправить партию tg=%s chat=%s: %r", user.telegram_user_id, user.chat_id, e)
            break
        db.mark_processed(user.id, [(game.url, game.end_time)])
        sent += 1

    if sent:
        log.info("tg=%s (%s): отправлено %d отчётов", user.telegram_user_id, user.chess_username, sent)
    return sent


async def check_new_games(bot: Bot, db: Database, now: datetime | None = None) -> int:
    """Одна проверка по всем пользователям. Возвращает общее число отправленных партий."""
    now = now or datetime.now(timezone.utc)
    users = db.list_users()

    by_username: dict[str, list[User]] = defaultdict(list)
    for u in users:
        by_username[u.chess_username].append(u)
    log.info("Проверка партий: %d пользователей, %d chess-аккаунтов", len(users), len(by_username))

    sent_total = 0
    # Stockfish запускается лениво — только если в этой проверке есть что анализировать
    async with Analyzer() as analyzer:
        cache = AnalysisCache(analyzer)
        for username, group in by_username.items():
            sent_total += await _check_username(bot, db, username, group, now, cache)

    log.info("Проверка завершена, отправлено отчётов: %d", sent_total)
    return sent_total


async def _check_username(
    bot: Bot, db: Database, username: str, group: list[User], now: datetime, cache: AnalysisCache
) -> int:
    """Скачивает партии одного chess-аккаунта и рассылает их всем его подписчикам."""
    since = min(u.updated_at for u in group)
    try:
        games = await _fetch_games(username, since, now)
    except chess_api.PlayerNotFound:
        log.warning("Игрок %s не найден на Chess.com (удалён/закрыт?), пропускаю", username)
        return 0
    except chess_api.ChessComError as e:
        log.warning("Не удалось получить партии %s: %s", username, e)
        return 0
    except Exception:
        log.exception("Неожиданная ошибка при получении партий %s", username)
        return 0

    sent = 0
    for user in group:
        # Username мог смениться через /setuser, пока мы качали архив или анализировали
        fresh = db.get_user(user.telegram_user_id)
        if fresh is None or fresh.chess_username != username:
            continue
        try:
            sent += await _process_user(bot, db, fresh, games, cache)
        except Exception:
            log.exception("Ошибка обработки партий для tg=%s", user.telegram_user_id)
    return sent


async def job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обёртка для JobQueue: db передаётся через job.data."""
    try:
        await check_new_games(context.bot, context.job.data)
    except Exception:
        log.exception("Фоновая проверка упала")
