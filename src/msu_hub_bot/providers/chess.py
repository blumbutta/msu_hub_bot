"""Bounded Lichess puzzle retrieval and standard-chess move validation."""

import asyncio
import io
import json
import random
import re
import time
from dataclasses import dataclass
from typing import cast

import aiohttp
import chess
import chess.pgn

from msu_hub_bot.providers.exceptions import ExternalServiceError

PUZZLE_URL = "https://lichess.org/api/puzzle/next"
FETCH_TIMEOUT = 7.0
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 100_000
MAX_PGN_LENGTH = 20_000
MAX_PLIES = 1000
RATE_LIMIT_COOLDOWN = 60.0
_request_lock: asyncio.Lock | None = None
_cooldown_until = 0.0

PIECE_NAMES = {
    chess.PAWN: "Пешка",
    chess.KNIGHT: "Конь",
    chess.BISHOP: "Слон",
    chess.ROOK: "Ладья",
    chess.QUEEN: "Ферзь",
    chess.KING: "Король",
}


@dataclass(frozen=True)
class MoveOption:
    uci: str
    label: str


@dataclass(frozen=True)
class Puzzle:
    id: str
    fen: str
    solution: tuple[str, ...]
    options: tuple[MoveOption, ...]
    line: tuple[str, ...]


class UnsuitablePuzzle(ValueError):
    """A response cannot safely be used as a six-choice chess question."""


class StrictGameBuilder(chess.pgn.GameBuilder[chess.pgn.Game]):
    def handle_error(self, error: Exception) -> None:
        # Do not accept a silently truncated PGN after a parse error.
        raise UnsuitablePuzzle("Invalid PGN") from error


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UnsuitablePuzzle("Expected an object")
    return cast(dict[str, object], value)


def move_label(board: chess.Board, move: chess.Move) -> str:
    """A readable label with an explicit origin, including underpromotions."""
    piece = board.piece_at(move.from_square)
    if piece is None or move not in board.legal_moves:
        raise UnsuitablePuzzle("Illegal answer move")
    name = "Рокировка" if board.is_castling(move) else PIECE_NAMES[piece.piece_type]
    arrow = "×" if board.is_capture(move) else "→"
    label = f"{name} {chess.square_name(move.from_square)} {arrow} {chess.square_name(move.to_square)}"
    if move.promotion is not None:
        label += f" = {PIECE_NAMES[move.promotion]}"
    return label


def parse_puzzle(data: object) -> Puzzle:
    response = _object(data)
    game_data, puzzle_data = _object(response.get("game")), _object(response.get("puzzle"))
    puzzle_id = puzzle_data.get("id")
    if not isinstance(puzzle_id, str) or re.fullmatch(r"[A-Za-z0-9]{5}", puzzle_id) is None:
        raise UnsuitablePuzzle("Invalid puzzle id")
    initial_ply = puzzle_data.get("initialPly")
    if type(initial_ply) is not int or not 0 <= initial_ply < MAX_PLIES:
        raise UnsuitablePuzzle("Invalid initial ply")
    pgn = game_data.get("pgn")
    if not isinstance(pgn, str) or not pgn or len(pgn) > MAX_PGN_LENGTH:
        raise UnsuitablePuzzle("Invalid PGN size")
    themes = puzzle_data.get("themes")
    if not isinstance(themes, list) or len(themes) > 100 or any(not isinstance(theme, str) for theme in themes):
        raise UnsuitablePuzzle("Invalid themes")
    if "mateIn1" in themes:
        raise UnsuitablePuzzle("Mate-in-one is unsuitable for this quiz")
    raw_solution = puzzle_data.get("solution")
    if not isinstance(raw_solution, list) or not 2 <= len(raw_solution) <= 40:
        raise UnsuitablePuzzle("Invalid solution length")
    solution: list[str] = []
    for value in raw_solution:
        if not isinstance(value, str) or re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", value) is None:
            raise UnsuitablePuzzle("Invalid solution move")
        solution.append(value)

    try:
        game = chess.pgn.read_game(io.StringIO(pgn), Visitor=StrictGameBuilder)
        if game is None or game.errors:
            raise UnsuitablePuzzle("Missing valid game")
        board = game.board()
        if type(board) is not chess.Board or board.chess960 or not board.is_valid():
            raise UnsuitablePuzzle("Only valid standard chess is supported")
        # API PGN contains the opponent's setup move at initialPly + 1.
        # The first solution move belongs to the player, not to that opponent.
        target_ply = initial_ply + 1
        for move in game.mainline_moves():
            if board.ply() >= target_ply:
                break
            board.push(move)
        if board.ply() != target_ply or not board.is_valid() or board.is_game_over():
            raise UnsuitablePuzzle("Missing playable puzzle position")
        supplied_fen = puzzle_data.get("fen")
        if supplied_fen is not None:
            if not isinstance(supplied_fen, str) or len(supplied_fen) > 200:
                raise UnsuitablePuzzle("Invalid supplied FEN")
            supplied = chess.Board(supplied_fen)
            if supplied.fen(en_passant="fen").split()[:4] != board.fen(en_passant="fen").split()[:4]:
                raise UnsuitablePuzzle("PGN and FEN disagree")
        legal = list(board.legal_moves)
        if len(legal) < 6:
            raise UnsuitablePuzzle("Not enough legal answers")
        continuation = board.copy()
        line: list[str] = []
        for uci in solution:
            move = chess.Move.from_uci(uci)
            if move not in continuation.legal_moves:
                raise UnsuitablePuzzle("Illegal solution")
            line.append(continuation.san(move))
            continuation.push(move)
            if len(line) == 1 and continuation.is_checkmate():
                raise UnsuitablePuzzle("Mate-in-one is unsuitable for this quiz")
        correct = chess.Move.from_uci(solution[0])
        alternatives = []
        for move in legal:
            if move == correct:
                continue
            after = board.copy(stack=False)
            after.push(move)
            if after.is_checkmate():
                raise UnsuitablePuzzle("An immediate mate contradicts the proposed best move")
            alternatives.append(move)
        if len(alternatives) < 5:
            raise UnsuitablePuzzle("Not enough legal distractors")
        choices = [correct, *random.sample(alternatives, 5)]
        random.shuffle(choices)
        options = tuple(MoveOption(move.uci(), move_label(board, move)) for move in choices)
        return Puzzle(puzzle_id, board.fen(), tuple(solution), options, tuple(line))
    except ValueError as exc:
        raise UnsuitablePuzzle("Invalid chess position or continuation") from exc


async def _request_json(session: aiohttp.ClientSession) -> object:
    global _request_lock, _cooldown_until
    if _cooldown_until > time.monotonic():
        raise ExternalServiceError("Источник шахматных задач временно ограничил запросы.")
    if _request_lock is None:
        _request_lock = asyncio.Lock()
    async with _request_lock:
        # Another chat may have received a rate limit while this call was queued.
        if _cooldown_until > time.monotonic():
            raise ExternalServiceError("Источник шахматных задач временно ограничил запросы.")
        async with session.get(PUZZLE_URL, params={"angle": "sacrifice"}, allow_redirects=False) as response:
            if response.status == 429:
                _cooldown_until = time.monotonic() + RATE_LIMIT_COOLDOWN
                raise ExternalServiceError("Источник шахматных задач временно ограничил запросы.")
            if response.status != 200:
                raise ExternalServiceError("Источник шахматных задач сейчас недоступен.")
            body = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ExternalServiceError("Источник вернул слишком большую шахматную задачу.")
            return cast(object, json.loads(body))


async def random_puzzle(recent_ids: tuple[str, ...] = ()) -> Puzzle:
    """Retrieve a fresh playable puzzle, including queue/retry time in the budget."""
    try:
        async with asyncio.timeout(FETCH_TIMEOUT):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                headers={"User-Agent": "MSUHubBot-Chess/1.0 (https://github.com/uburuntu/msu_hub_bot)"},
            ) as session:
                for _ in range(MAX_ATTEMPTS):
                    data = await _request_json(session)
                    try:
                        puzzle = parse_puzzle(data)
                    except UnsuitablePuzzle:
                        continue
                    if puzzle.id not in recent_ids:
                        return puzzle
        raise ExternalServiceError("Подходящей новой шахматной задачи не нашлось.")
    except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError) as exc:
        raise ExternalServiceError("Не удалось получить шахматную задачу. Попробуй позже.") from exc
