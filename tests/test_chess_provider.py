import asyncio
import copy
import json
import random

import aiohttp
import chess
import pytest

from msu_hub_bot.providers import chess as source
from msu_hub_bot.providers.exceptions import ExternalServiceError


# Public API specification fixture: setup Kh7 is ply56, puzzle initialPly is55.
PGN = (
    "d4 Nf6 c4 c5 Nf3 g6 Nc3 Bg7 e4 O-O d5 d6 Be2 a6 O-O e6 Bg5 h6 Bh4 g5 Bg3 Nh5 "
    "dxe6 Nxg3 exf7+ Rxf7 hxg3 Nc6 Qd2 Nd4 Rad1 Be5 Na4 b5 cxb5 axb5 Nxe5 dxe5 "
    "Nxc5 Rxa2 Nb3 Nxe2+ Qxe2 Qb6 Rd5 Bd7 Rfd1 Bc6 Rxe5 Ra4 Qh5 Qxf2+ Kh1 Bxe4 Rd8+ Kh7"
)
SOLUTION = ["d8h8", "h7h8", "h5h6", "e4h7", "e5e8", "f7f8", "e8f8", "f2f8", "h6f8"]
TACTICAL_FEN = "1r3r2/3p1p1k/1q1PpQpp/3p4/8/Pp3R1R/2P3PP/2K5 w - - 1 2"


def payload(puzzle_id="iSz4O"):
    return {
        "game": {"pgn": PGN},
        "puzzle": {"id": puzzle_id, "initialPly": 55, "solution": SOLUTION.copy(), "themes": ["sacrifice", "attraction"]},
    }


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(source, "_request_lock", None)
    monkeypatch.setattr(source, "_cooldown_until", 0.0)


class Response:
    def __init__(self, value=None, *, status=200, body=None, gate=None, tracker=None):
        self.status = status
        self.body = body if body is not None else json.dumps(value).encode()
        self.gate = gate
        self.tracker = tracker
        self.content = self
        self.entered = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        self.entered.set()
        if self.tracker is not None:
            self.tracker["active"] += 1
            self.tracker["maximum"] = max(self.tracker["maximum"], self.tracker["active"])
        return self

    async def __aexit__(self, *_):
        self.closed = True
        if self.tracker is not None:
            self.tracker["active"] -= 1

    async def iter_chunked(self, size):
        if self.gate is not None:
            await self.gate.wait()
        for offset in range(0, len(self.body), size):
            await asyncio.sleep(0)
            yield self.body[offset : offset + size]


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def install_session(monkeypatch, responses):
    session = Session(responses)
    monkeypatch.setattr(source.aiohttp, "ClientSession", lambda **kwargs: session)
    return session


def test_api_ply_is_opponents_setup_and_solution_starts_with_players_move():
    puzzle = source.parse_puzzle(payload())
    board = chess.Board(puzzle.fen)
    assert board.ply() == 56
    assert board.turn == chess.WHITE
    assert board.king(chess.BLACK) == chess.H7
    assert puzzle.solution == tuple(SOLUTION)
    assert puzzle.line == ("Rh8+", "Kxh8", "Qxh6+", "Bh7", "Re8+", "Rf8", "Rxf8+", "Qxf8", "Qxf8+")
    assert len(puzzle.options) == len({option.uci for option in puzzle.options}) == 6
    assert sum(option.uci == SOLUTION[0] for option in puzzle.options) == 1
    for option in puzzle.options:
        move = chess.Move.from_uci(option.uci)
        assert move in board.legal_moves
        assert option.label == source.move_label(board, move)
    for uci in puzzle.solution:
        move = chess.Move.from_uci(uci)
        assert move in board.legal_moves
        board.push(move)
    assert board.is_check() and not board.pieces(chess.QUEEN, chess.BLACK)


def test_capture_solution_keeps_other_captures_in_the_displayed_answers():
    data = payload()
    data["game"]["pgn"] = '[SetUp "1"]\n[FEN "1r3r1k/3p1p2/1q1PpQpp/3p4/8/Pp3R1R/2P3PP/2K5 b - - 0 1"]\n\nKh7'
    data["puzzle"].update(initialPly=1, solution=["h3h6", "h7g8"])
    puzzle = source.parse_puzzle(data)
    assert puzzle.fen == TACTICAL_FEN
    board = chess.Board(puzzle.fen)
    assert sum(board.is_capture(chess.Move.from_uci(option.uci)) for option in puzzle.options) == 3
    assert all(" → " in option.label and "×" not in option.label for option in puzzle.options)
    assert sum(option.uci == "h3h6" for option in puzzle.options) == 1


@pytest.mark.parametrize(
    ("fen", "uci", "capture_count"),
    [
        (TACTICAL_FEN, "h3h6", 3),
        (TACTICAL_FEN, "f3f2", 3),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "a1a8", 2),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", 2),
        ("5Q2/8/8/1k6/8/3NB1pN/R4n2/7K w - - 0 1", "f8f2", 4),
        ("5Q2/8/8/1k6/8/3NB1pN/R4n2/7K w - - 0 1", "h1g2", 4),
        (chess.STARTING_FEN, "e2e4", 0),
        ("8/8/8/3pP3/8/8/7k/R4K2 w - d6 0 1", "e5e6", 0),
        ("5Q2/8/8/1k6/8/3NB1pN/R4n2/3N2BK w - - 0 1", "f8f2", 6),
        ("5Q2/8/8/1k6/8/3NBppN/R4n2/3N2BK w - - 0 1", "h3f2", 6),
    ],
)
def test_answer_mix_depends_on_the_position_not_the_correct_move(fen, uci, capture_count):
    board = chess.Board(fen)
    assert board.is_valid()
    legal = list(board.legal_moves)
    correct = chess.Move.from_uci(uci)
    choices = source.select_moves(board, correct, legal)
    assert len(choices) == len(set(choices)) == 6
    assert choices.count(correct) == 1
    assert all(move in board.legal_moves for move in choices)
    assert sum(board.is_capture(move) for move in choices) == capture_count
    assert list(board.legal_moves) == legal
    assert board.fen() == fen


@pytest.mark.parametrize(
    ("fen", "uci"),
    [
        ("8/8/8/3pP3/8/8/7k/R4K2 w - d6 0 1", "e5d6"),
        ("5Q2/8/8/1k6/8/3NB1pN/R4n2/3N2BK w - - 0 1", "h1g2"),
        ("8/8/8/3pP3/P7/8/7k/5K2 w - d6 0 1", "e5d6"),
        ("8/8/8/3pP3/P7/8/7k/5K2 w - d6 0 1", "e5e6"),
        ("5Q2/8/8/1k6/8/3NB1pN/R3nn2/5R1K w - - 0 1", "f1f2"),
        ("5Q2/8/8/1k6/8/3NB1pN/R3nn2/5R1K w - - 0 1", "h1g2"),
    ],
)
def test_sparse_positions_cannot_make_a_singleton_move_type_a_clue(fen, uci):
    board = chess.Board(fen)
    assert board.is_valid()
    correct = chess.Move.from_uci(uci)
    assert correct in board.legal_moves
    with pytest.raises(source.UnsuitablePuzzle):
        source.select_moves(board, correct, list(board.legal_moves))


@pytest.mark.parametrize("uci", ["h3h6", "f3f2"])
def test_correct_position_and_distractors_vary_for_capture_and_quiet_answers(monkeypatch, uci):
    board = chess.Board(TACTICAL_FEN)
    correct = chess.Move.from_uci(uci)
    positions = set()
    alternatives = set()
    for seed in range(60):
        rng = random.Random(seed)
        monkeypatch.setattr(source.random, "sample", rng.sample)
        monkeypatch.setattr(source.random, "shuffle", rng.shuffle)
        choices = source.select_moves(board, correct, list(board.legal_moves))
        assert sum(board.is_capture(move) for move in choices) == 3
        positions.add(choices.index(correct))
        alternatives.add(frozenset(move for move in choices if move != correct))
    assert positions == set(range(6))
    assert len(alternatives) > 1


def test_en_passant_counts_as_a_capture_when_balancing_answers():
    board = chess.Board()
    for uci in ("e2e4", "a7a6", "f1c4", "h7h6", "e4e5", "d7d5"):
        board.push_uci(uci)
    correct = chess.Move.from_uci("e5d6")
    assert board.is_en_passant(correct)
    choices = source.select_moves(board, correct, list(board.legal_moves))
    assert correct in choices
    assert sum(board.is_capture(move) for move in choices) == 3
    assert source.move_label(board, correct) == "Пешка e5 → d6"


def test_full_game_pgn_is_stopped_at_puzzle_position():
    data = payload()
    data["game"]["pgn"] += " Rh8+ Kxh8"
    assert source.parse_puzzle(data).fen == source.parse_puzzle(payload()).fen


def test_black_to_move_position_and_san_are_preserved():
    data = payload()
    data["game"]["pgn"] = "e4"
    data["puzzle"]["initialPly"] = 0
    data["puzzle"]["solution"] = ["e7e5", "g1f3"]
    puzzle = source.parse_puzzle(data)
    assert chess.Board(puzzle.fen).turn == chess.BLACK
    assert puzzle.line == ("e5", "Nf3")
    assert "e7e5" in {option.uci for option in puzzle.options}


def test_alternative_immediate_mate_rejects_the_entire_puzzle():
    data = payload()
    data["game"]["pgn"] = '[SetUp "1"]\n[FEN "7k/8/5QK1/8/8/8/8/8 b - - 0 1"]\n\nKg8'
    data["puzzle"]["initialPly"] = 1
    data["puzzle"]["solution"] = ["f6e6", "g8h8"]
    with pytest.raises(source.UnsuitablePuzzle):
        source.parse_puzzle(data)


@pytest.mark.parametrize("variant", ["Chess960", "Atomic", "Crazyhouse"])
def test_chess_variants_are_rejected(variant):
    data = payload()
    data["game"]["pgn"] = f'[Variant "{variant}"]\n\n' + PGN
    with pytest.raises(source.UnsuitablePuzzle):
        source.parse_puzzle(data)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("puzzle", "id", "../fake"),
        ("puzzle", "initialPly", True),
        ("puzzle", "initialPly", -1),
        ("puzzle", "initialPly", 2000),
        ("puzzle", "initialPly", 54),
        ("puzzle", "initialPly", 57),
        ("puzzle", "solution", []),
        ("puzzle", "solution", ["d8h8"]),
        ("puzzle", "solution", ["d8h8"] * 41),
        ("puzzle", "solution", ["d8h8", "h7h6"]),
        ("puzzle", "solution", ["d8d7", "a1a2"]),
        ("puzzle", "solution", [None, "h7h8"]),
        ("puzzle", "solution", ["0000", "h7h8"]),
        ("puzzle", "themes", ["mateIn1"]),
        ("puzzle", "themes", "sacrifice"),
        ("puzzle", "themes", [None]),
        ("puzzle", "fen", chess.STARTING_FEN),
        ("puzzle", "fen", 123),
        ("game", "pgn", ""),
        ("game", "pgn", "e4 e5"),
        ("game", "pgn", "Nf3 Nf6 Qh5"),
        ("game", "pgn", " " * (source.MAX_PGN_LENGTH + 1)),
    ],
)
def test_unusable_provider_payload_is_rejected(section, field, value):
    data = payload()
    data[section][field] = value
    with pytest.raises(source.UnsuitablePuzzle):
        source.parse_puzzle(data)


@pytest.mark.parametrize("data", [None, [], {}, {"game": [], "puzzle": {}}])
def test_wrong_response_shape_is_rejected(data):
    with pytest.raises(source.UnsuitablePuzzle):
        source.parse_puzzle(data)


def test_optional_fen_must_agree_with_replayed_pgn():
    data = payload()
    data["puzzle"]["fen"] = source.parse_puzzle(data).fen
    assert source.parse_puzzle(data).id == "iSz4O"


@pytest.mark.parametrize("en_passant", ["legal", "fen"])
def test_optional_fen_accepts_equivalent_non_capturable_en_passant(en_passant):
    data = payload()
    data["game"]["pgn"] = "e4"
    data["puzzle"]["initialPly"] = 0
    data["puzzle"]["solution"] = ["e7e5", "g1f3"]
    board = chess.Board()
    board.push_uci("e2e4")
    data["puzzle"]["fen"] = board.fen(en_passant=en_passant)
    assert source.parse_puzzle(data).fen == board.fen()


@pytest.mark.parametrize("change", ["en_passant", "castling", "invalid"])
def test_optional_fen_rejects_changed_legal_move_state(change):
    data = payload()
    data["game"]["pgn"] = "e4 a6 Bc4 h6 e5 d5"
    data["puzzle"]["initialPly"] = 5
    data["puzzle"]["solution"] = ["e5d6", "e7d6"]
    board = chess.Board(source.parse_puzzle(data).fen)
    if change == "en_passant":
        board.ep_square = None
    elif change == "castling":
        board.castling_rights = chess.BB_EMPTY
    else:
        board.ep_square = chess.E4
    data["puzzle"]["fen"] = board.fen(en_passant="fen")
    with pytest.raises(source.UnsuitablePuzzle):
        source.parse_puzzle(data)


def test_game_over_and_too_few_legal_moves_are_rejected():
    data = payload()
    data["game"]["pgn"] = "f3 e5 g4 Qh4#"
    data["puzzle"]["initialPly"] = 3
    with pytest.raises(source.UnsuitablePuzzle):
        source.parse_puzzle(data)
    data["game"]["pgn"] = '[SetUp "1"]\n[FEN "8/8/8/8/8/6k1/6q1/7K b - - 0 1"]\n\nQf2'
    data["puzzle"]["initialPly"] = 1
    with pytest.raises(source.UnsuitablePuzzle):
        source.parse_puzzle(data)


@pytest.mark.parametrize(
    ("fen", "uci", "label"),
    [
        (chess.STARTING_FEN, "g1f3", "Конь g1 → f3"),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", "Рокировка e1 → g1"),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "a1a8", "Ладья a1 → a8"),
        ("8/P7/8/8/8/8/7k/5K2 w - - 0 1", "a7a8n", "Пешка a7 → a8 = Конь"),
        ("8/8/8/3pP3/8/8/7k/5K2 w - d6 0 1", "e5d6", "Пешка e5 → d6"),
    ],
)
def test_explicit_move_labels_cover_castling_capture_promotion_and_en_passant(fen, uci, label):
    assert source.move_label(chess.Board(fen), chess.Move.from_uci(uci)) == label


async def test_fresh_puzzle_fetch_uses_public_endpoint_and_cleans_up(monkeypatch):
    response = Response(payload())
    session = install_session(monkeypatch, [response])
    result = await source.random_puzzle()
    assert result.id == "iSz4O"
    assert session.calls == [(source.PUZZLE_URL, {"params": {"angle": "mix"}, "allow_redirects": False})]
    assert session.closed and response.closed


async def test_recent_ids_and_unsuitable_puzzles_are_skipped_with_bounded_retries(monkeypatch):
    invalid = copy.deepcopy(payload())
    invalid["puzzle"]["themes"] = ["mateIn1"]
    session = install_session(monkeypatch, [Response(payload("aaaaa")), Response(invalid), Response(payload("bbbbb"))])
    result = await source.random_puzzle(("aaaaa",))
    assert result.id == "bbbbb"
    assert len(session.calls) == 3


async def test_all_recent_puzzles_fail_instead_of_repeating(monkeypatch):
    session = install_session(monkeypatch, [Response(payload()) for _ in range(3)])
    with pytest.raises(ExternalServiceError):
        await source.random_puzzle(("iSz4O",))
    assert len(session.calls) == 3 and session.closed


@pytest.mark.parametrize("status", [301, 403, 500])
async def test_http_failures_do_not_retry_or_follow_redirects(monkeypatch, status):
    response = Response(status=status)
    session = install_session(monkeypatch, [response])
    with pytest.raises(ExternalServiceError):
        await source.random_puzzle()
    assert len(session.calls) == 1 and session.closed and response.closed


@pytest.mark.parametrize("body", [b"not-json", b"x" * (source.MAX_RESPONSE_BYTES + 1)])
async def test_bad_or_oversized_response_is_bounded_and_normalized(monkeypatch, body):
    response = Response(body=body)
    session = install_session(monkeypatch, [response])
    with pytest.raises(ExternalServiceError):
        await source.random_puzzle()
    assert session.closed and response.closed


async def test_transport_failure_is_normalized_and_session_closed(monkeypatch):
    session = install_session(monkeypatch, [aiohttp.ClientConnectionError("offline")])
    with pytest.raises(ExternalServiceError):
        await source.random_puzzle()
    assert session.closed


async def test_rate_limit_sets_global_minute_cooldown_and_next_call_fails_promptly(monkeypatch):
    response = Response(status=429)
    session = install_session(monkeypatch, [response])
    before = source.time.monotonic()
    with pytest.raises(ExternalServiceError):
        await source.random_puzzle()
    assert source._cooldown_until >= before + 60
    with pytest.raises(ExternalServiceError):
        await asyncio.wait_for(source.random_puzzle(), timeout=0.1)
    assert len(session.calls) == 1 and response.closed
    monkeypatch.setattr(source, "_cooldown_until", 0)
    session.responses.append(Response(payload()))
    assert (await source.random_puzzle()).id == "iSz4O"


async def test_requests_from_chats_are_serialized(monkeypatch):
    tracker = {"active": 0, "maximum": 0}
    session = install_session(monkeypatch, [Response(payload(), tracker=tracker), Response(payload("abcde"), tracker=tracker)])
    first, second = await asyncio.gather(source.random_puzzle(), source.random_puzzle())
    assert {first.id, second.id} == {"iSz4O", "abcde"}
    assert len(session.calls) == 2 and tracker == {"active": 0, "maximum": 1}


async def test_queue_wait_counts_toward_whole_fetch_deadline(monkeypatch):
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(source, "_request_lock", lock)
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.02)
    session = install_session(monkeypatch, [])
    try:
        with pytest.raises(ExternalServiceError):
            await source.random_puzzle()
        assert not session.calls and session.closed
    finally:
        lock.release()


async def test_timeout_and_external_cancellation_release_response_session_and_lock(monkeypatch):
    for cancel in (False, True):
        response = Response(payload(), gate=asyncio.Event())
        session = install_session(monkeypatch, [response])
        monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.02 if not cancel else 7)
        task = asyncio.create_task(source.random_puzzle())
        await response.entered.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ExternalServiceError):
            await task
        assert session.closed and response.closed
        assert not source._request_lock.locked()


async def test_cooldown_is_rechecked_after_waiting_for_other_chat(monkeypatch):
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(source, "_request_lock", lock)
    session = install_session(monkeypatch, [])
    task = asyncio.create_task(source.random_puzzle())
    await asyncio.sleep(0)
    monkeypatch.setattr(source, "_cooldown_until", source.time.monotonic() + 60)
    lock.release()
    with pytest.raises(ExternalServiceError):
        await task
    assert not session.calls and session.closed
