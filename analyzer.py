"""Анализ партий локальным Stockfish через UCI (python-chess, асинхронный chess.engine).

Каждая позиция партии оценивается один раз на глубину STOCKFISH_DEPTH. Для каждого хода считаются:
- centipawn loss — насколько ухудшилась оценка с точки зрения сходившего (оценки обрезаются
  до ±1000 cp, чтобы мат/«мат в N» не давали потерь в десятки тысяч);
- падение «вероятности выигрыша» (формула Lichess) — по нему ход классифицируется как
  неточность / ошибка / зевок. Так ход «+8 → +5» в выигранной позиции не считается зевком,
  хотя потеря в cp большая;
- совпал ли ход с лучшим ходом Stockfish и, если включён STOCKFISH_FIND_ONLY_MOVES (MultiPV 2),
  насколько хуже был второй по силе ход — так находятся «единственные ходы».
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
# «Хороший» ход — не лучший, но теряет меньше GOOD_MAX_DROP win%
GOOD_MAX_DROP = 2
# «Единственный ход»: второй по силе ход хуже минимум на ONLY_MOVE_MIN_GAP win%,
# и позиция ещё не решена (оценка до хода в пределах ±ONLY_MOVE_MAX_CP)
ONLY_MOVE_MIN_GAP = 20
ONLY_MOVE_MAX_CP = 400


class AnalysisError(Exception):
    """Не удалось проанализировать партию (Stockfish не запустился/упал/завис, битый PGN)."""


def win_percent(cp: int) -> float:
    """Вероятность выигрыша по оценке в cp (формула Lichess)."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(win_drop: float) -> float:
    """Точность хода 0–100 по падению win% (формула Lichess)."""
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * win_drop) - 3.1669))


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
    best_san: str | None  # None, если сыгран лучший ход
    before: Eval  # оценка до хода (= оценка лучшего хода)
    after: Eval
    cp_loss: int
    win_drop: float
    second: Eval | None = None  # оценка второго по силе хода (только с MultiPV 2)
    is_recapture: bool = False  # взятие сразу после взятия соперника (размен)

    @property
    def label(self) -> str:
        """Номер хода в нотации: «12.» для белых, «12...» для чёрных."""
        return f"{self.ply // 2 + 1}{'.' if self.color == chess.WHITE else '...'}"

    @property
    def is_best(self) -> bool:
        return self.best_san is None

    @property
    def category(self) -> str | None:
        if self.win_drop >= BLUNDER:
            return "blunder"
        if self.win_drop >= MISTAKE:
            return "mistake"
        if self.win_drop >= INACCURACY:
            return "inaccuracy"
        return None

    @property
    def only_move_gap(self) -> float:
        """На сколько win% второй по силе ход хуже сыгранного, если это «единственный ход», иначе 0.

        Не считаются: не лучший ход, вынужденный ход (второго варианта нет), взятие в ответ
        на взятие (размен — очевидный ход) и ходы в уже решённой позиции.
        """
        if not self.is_best or self.second is None or self.is_recapture:
            return 0.0
        mine = self.before.pov(self.color)
        if abs(mine) > ONLY_MOVE_MAX_CP:
            return 0.0
        gap = win_percent(mine) - win_percent(self.second.pov(self.color))
        return gap if gap >= ONLY_MOVE_MIN_GAP else 0.0


@dataclass(frozen=True)
class Highlight:
    """Сильный ход игрока для отчёта."""
    kind: str  # "only" — единственный ход, "punish" — наказал за ошибку соперника
    move: MoveAnalysis
    opponent_move: MoveAnalysis | None = None  # ошибка соперника, за которую наказали


@dataclass
class SideSummary:
    inaccuracies: int = 0
    mistakes: int = 0
    blunders: int = 0
    best: int = 0  # совпали с лучшим ходом Stockfish
    good: int = 0  # не лучшие, но почти без потерь
    acpl: int = 0  # средняя потеря в cp
    accuracy: float = 0.0  # 0–100
    worst: list[MoveAnalysis] = field(default_factory=list)
    highlights: list[Highlight] = field(default_factory=list)


@dataclass
class GameAnalysis:
    depth: int
    moves: list[MoveAnalysis]

    def summary(self, color: chess.Color, worst_count: int = 3, highlight_count: int = 3) -> SideSummary:
        own = [m for m in self.moves if m.color == color]
        s = SideSummary()
        if not own:
            return s

        for m in own:
            if m.category == "blunder":
                s.blunders += 1
            elif m.category == "mistake":
                s.mistakes += 1
            elif m.category == "inaccuracy":
                s.inaccuracies += 1
            elif m.is_best:
                s.best += 1
            elif m.win_drop < GOOD_MAX_DROP:
                s.good += 1
        s.acpl = round(sum(m.cp_loss for m in own) / len(own))
        s.accuracy = sum(move_accuracy(m.win_drop) for m in own) / len(own)

        ranked = sorted((m for m in own if m.cp_loss > 0), key=lambda m: (m.win_drop, m.cp_loss), reverse=True)
        s.worst = sorted(ranked[:worst_count], key=lambda m: m.ply)

        # Сильные ходы: сначала самые «узкие» единственные ходы, затем наказания за самые крупные ошибки
        only, punish = [], []
        for i, m in enumerate(self.moves):
            if m.color != color:
                continue
            prev = self.moves[i - 1] if i else None
            if m.only_move_gap:
                only.append(Highlight("only", m))
            elif m.is_best and prev is not None and prev.category in ("mistake", "blunder"):
                punish.append(Highlight("punish", m, prev))
        only.sort(key=lambda h: h.move.only_move_gap, reverse=True)
        punish.sort(key=lambda h: h.opponent_move.win_drop, reverse=True)
        s.highlights = sorted((only + punish)[:highlight_count], key=lambda h: h.move.ply)
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


@dataclass(frozen=True)
class _Position:
    """Результат анализа одной позиции."""
    eval: Eval
    best: chess.Move | None = None
    second: Eval | None = None


class Analyzer:
    """Обёртка над процессом Stockfish. Процесс запускается лениво и перезапускается после сбоя.

    Использование:
        async with Analyzer() as analyzer:
            result = await analyzer.analyse_pgn(pgn)
    """

    def __init__(self):
        self._engine: chess.engine.UciProtocol | None = None
        self._limit = chess.engine.Limit(depth=config.STOCKFISH_DEPTH, time=config.STOCKFISH_MOVE_TIME_LIMIT)
        # Два варианта нужны, чтобы понять, был ли сыгранный ход единственным
        self._multipv = 2 if config.STOCKFISH_FIND_ONLY_MOVES else 1

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

    async def _evaluate(self, engine: chess.engine.UciProtocol, board: chess.Board) -> _Position:
        terminal = _terminal_eval(board)
        if terminal:
            return _Position(terminal)
        infos = await engine.analyse(board, self._limit, multipv=self._multipv)
        if not infos or infos[0].get("score") is None:
            raise AnalysisError("Stockfish не вернул оценку позиции")
        pv = infos[0].get("pv") or [None]
        second = None
        if len(infos) > 1 and infos[1].get("score") is not None:
            second = _eval_from_score(infos[1]["score"])
        return _Position(_eval_from_score(infos[0]["score"]), pv[0], second)

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
        pos = await self._evaluate(engine, board)
        prev_was_capture = False
        for ply, move in enumerate(moves):
            color = board.turn
            san = board.san(move)
            best_san = board.san(pos.best) if pos.best is not None and pos.best != move else None
            is_capture = board.is_capture(move)
            is_recapture = is_capture and prev_was_capture
            prev_was_capture = is_capture
            board.push(move)
            nxt = await self._evaluate(engine, board)

            before, after = pos.eval, nxt.eval
            cp_loss = max(0, before.pov(color) - after.pov(color))
            win_drop = max(0.0, win_percent(before.pov(color)) - win_percent(after.pov(color)))
            result.append(
                MoveAnalysis(ply, color, san, best_san, before, after, cp_loss, win_drop, pos.second, is_recapture)
            )
            pos = nxt
        return GameAnalysis(depth=config.STOCKFISH_DEPTH, moves=result)
