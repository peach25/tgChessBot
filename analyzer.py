"""Анализ партий локальным Stockfish через UCI (python-chess, асинхронный chess.engine).

Каждая позиция партии оценивается один раз на глубину STOCKFISH_DEPTH. Для каждого хода считаются:
- centipawn loss — насколько ухудшилась оценка с точки зрения сходившего (оценки обрезаются
  до ±1000 cp, чтобы мат/«мат в N» не давали потерь в десятки тысяч);
- падение «вероятности выигрыша» (формула Lichess) — по нему ход классифицируется как
  неточность / ошибка / зевок. Так ход «+8 → +5» в выигранной позиции не считается зевком,
  хотя потеря в cp большая.
"""
import asyncio
import io
import logging
import math
from dataclasses import dataclass, field

import chess
import chess.engine
import chess.pgn

import config

log = logging.getLogger(__name__)

CP_CLAMP = 1000
MATE_CP = 10_000
# Падение win% (0–100), начиная с которого ход попадает в категорию
INACCURACY, MISTAKE, BLUNDER = 10, 20, 30


class AnalysisError(Exception):
    """Не удалось проанализировать партию (Stockfish не запустился/упал/завис, битый PGN)."""


def win_percent(cp: int) -> float:
    """Вероятность выигрыша по оценке в cp (формула Lichess)."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


@dataclass(frozen=True)
class Eval:
    """Оценка позиции; все поля — с точки зрения белых."""
    cp: int  # мат → ±MATE_CP
    mate: int | None = None  # мат в N ходов: >0 — матуют белые, <0 — чёрные
    checkmate: bool = False  # позиция — уже мат

    def pov(self, color: chess.Color) -> int:
        cp = max(-CP_CLAMP, min(CP_CLAMP, self.cp))
        return cp if color == chess.WHITE else -cp

    def text(self, color: chess.Color = chess.WHITE) -> str:
        """Оценка для показа с точки зрения color: «+1.2» — лучше для него, «M3» — он матует в 3."""
        if self.checkmate:
            return "#"
        sign = 1 if color == chess.WHITE else -1
        if self.mate is not None:
            m = self.mate * sign
            return f"M{m}" if m > 0 else f"-M{-m}"
        return f"{self.cp * sign / 100:+.1f}"


@dataclass(frozen=True)
class MoveAnalysis:
    ply: int  # 0 — первый ход белых
    color: chess.Color
    san: str
    best_san: str | None
    before: Eval
    after: Eval
    cp_loss: int
    win_drop: float

    @property
    def label(self) -> str:
        """Номер хода в нотации: «12.» для белых, «12...» для чёрных."""
        return f"{self.ply // 2 + 1}{'.' if self.color == chess.WHITE else '...'}"

    @property
    def category(self) -> str | None:
        if self.win_drop >= BLUNDER:
            return "blunder"
        if self.win_drop >= MISTAKE:
            return "mistake"
        if self.win_drop >= INACCURACY:
            return "inaccuracy"
        return None


@dataclass
class SideSummary:
    inaccuracies: int = 0
    mistakes: int = 0
    blunders: int = 0
    acpl: int = 0  # средняя потеря в cp
    worst: list[MoveAnalysis] = field(default_factory=list)


@dataclass
class GameAnalysis:
    depth: int
    moves: list[MoveAnalysis]

    def summary(self, color: chess.Color, worst_count: int = 3) -> SideSummary:
        own = [m for m in self.moves if m.color == color]
        s = SideSummary()
        for m in own:
            if m.category == "blunder":
                s.blunders += 1
            elif m.category == "mistake":
                s.mistakes += 1
            elif m.category == "inaccuracy":
                s.inaccuracies += 1
        if own:
            s.acpl = round(sum(m.cp_loss for m in own) / len(own))
        ranked = sorted((m for m in own if m.cp_loss > 0), key=lambda m: (m.win_drop, m.cp_loss), reverse=True)
        s.worst = sorted(ranked[:worst_count], key=lambda m: m.ply)
        return s


def _eval_from_score(score: chess.engine.PovScore) -> Eval:
    white = score.white()
    mate = white.mate()
    if mate is not None:
        return Eval(cp=MATE_CP if mate > 0 else -MATE_CP, mate=mate)
    return Eval(cp=white.score())


def _terminal_eval(board: chess.Board) -> Eval | None:
    """Оценка конечной позиции без движка (мат/пат/ничья)."""
    if board.is_checkmate():
        # Заматован тот, чей ход
        return Eval(cp=-MATE_CP if board.turn == chess.WHITE else MATE_CP, checkmate=True)
    if board.is_stalemate() or board.is_insufficient_material():
        return Eval(cp=0)
    return None


class Analyzer:
    """Обёртка над процессом Stockfish. Процесс запускается лениво и перезапускается после сбоя.

    Использование:
        async with Analyzer() as analyzer:
            result = await analyzer.analyse_pgn(pgn)
    """

    def __init__(self):
        self._engine: chess.engine.UciProtocol | None = None
        self._limit = chess.engine.Limit(depth=config.STOCKFISH_DEPTH, time=config.STOCKFISH_MOVE_TIME_LIMIT)

    async def __aenter__(self) -> "Analyzer":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _ensure_engine(self) -> chess.engine.UciProtocol:
        if self._engine is not None and not self._engine.returncode.done():
            return self._engine
        try:
            _, engine = await asyncio.wait_for(
                chess.engine.popen_uci(config.STOCKFISH_PATH), timeout=config.STOCKFISH_START_TIMEOUT
            )
            await engine.configure({"Threads": config.STOCKFISH_THREADS, "Hash": config.STOCKFISH_HASH_MB})
        except (OSError, chess.engine.EngineError, asyncio.TimeoutError) as e:
            raise AnalysisError(f"Не удалось запустить Stockfish ({config.STOCKFISH_PATH}): {e!r}") from e
        log.info("Stockfish запущен: %s", engine.id.get("name", config.STOCKFISH_PATH))
        self._engine = engine
        return engine

    async def close(self) -> None:
        if self._engine is None:
            return
        engine, self._engine = self._engine, None
        try:
            await asyncio.wait_for(engine.quit(), timeout=5)
        except Exception:
            log.warning("Stockfish не завершился штатно, убиваю процесс")
            try:
                engine.transport.kill()
            except Exception:
                pass

    async def _evaluate(self, engine: chess.engine.UciProtocol, board: chess.Board) -> tuple[Eval, chess.Move | None]:
        terminal = _terminal_eval(board)
        if terminal:
            return terminal, None
        info = await engine.analyse(board, self._limit)
        score = info.get("score")
        if score is None:
            raise AnalysisError("Stockfish не вернул оценку позиции")
        pv = info.get("pv") or [None]
        return _eval_from_score(score), pv[0]

    async def analyse_pgn(self, pgn: str) -> GameAnalysis:
        try:
            game = chess.pgn.read_game(io.StringIO(pgn))
        except Exception as e:
            raise AnalysisError(f"Не удалось распарсить PGN: {e!r}") from e
        if game is None or game.errors:
            raise AnalysisError(f"Некорректный PGN: {game.errors if game else 'пусто'}")

        board = game.board()
        moves = list(game.mainline_moves())
        if not moves:
            raise AnalysisError("В партии нет ходов")

        engine = await self._ensure_engine()
        try:
            return await asyncio.wait_for(
                self._analyse_moves(engine, board, moves), timeout=config.STOCKFISH_GAME_TIMEOUT
            )
        except (chess.engine.EngineError, chess.engine.EngineTerminatedError, asyncio.TimeoutError) as e:
            # Движок в неизвестном состоянии — перезапустим для следующей партии
            await self.close()
            raise AnalysisError(f"Stockfish не ответил: {e!r}") from e

    async def _analyse_moves(
        self, engine: chess.engine.UciProtocol, board: chess.Board, moves: list[chess.Move]
    ) -> GameAnalysis:
        result: list[MoveAnalysis] = []
        before, best = await self._evaluate(engine, board)
        for ply, move in enumerate(moves):
            color = board.turn
            san = board.san(move)
            best_san = board.san(best) if best is not None and best != move else None
            board.push(move)
            after, next_best = await self._evaluate(engine, board)

            cp_loss = max(0, before.pov(color) - after.pov(color))
            win_drop = max(0.0, win_percent(before.pov(color)) - win_percent(after.pov(color)))
            result.append(MoveAnalysis(ply, color, san, best_san, before, after, cp_loss, win_drop))
            before, best = after, next_best
        return GameAnalysis(depth=config.STOCKFISH_DEPTH, moves=result)
