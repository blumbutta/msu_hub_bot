"""Real PNG rendering and captured-piece history for shared chess games."""

import io
import xml.etree.ElementTree as ET

import chess
import pytest
from PIL import Image

from msu_hub_bot.commands.chess_play_game import Game, Player
from msu_hub_bot.media import chess_play_board
from msu_hub_bot.media.chess_play_board import HEIGHT, WIDTH, captured_pieces, render_match


def game(*moves, fen=chess.STARTING_FEN):
    result = Game(
        token="012345abcdef",
        bot_id=42,
        chat_id=-10012,
        white=Player(user_id=1, name="White"),
        created_at=1000,
        invite_deadline=1600,
        initial_fen=fen,
    )
    result.join(Player(user_id=2, name="Black"), 1100)
    for move in moves:
        result.move(result.turn_player.user_id, move, 1100)
    return result


def test_captures_follow_history_not_missing_starting_pieces():
    state = game("e2e4", "d7d5", "e4d5", "d8d5")
    white, black = captured_pieces(state)
    assert white == (chess.Piece(chess.PAWN, chess.BLACK),)
    assert black == (chess.Piece(chess.PAWN, chess.WHITE),)
    # A composed position must not claim all initially absent pieces were taken.
    assert captured_pieces(game(fen="7k/8/8/8/8/8/8/KR6 w - - 0 1")) == ((), ())


def test_en_passant_records_the_taken_pawn_even_though_destination_is_empty():
    state = game("e2e4", "a7a6", "e4e5", "d7d5", "e5d6")
    assert captured_pieces(state) == ((chess.Piece(chess.PAWN, chess.BLACK),), ())


def test_promoted_piece_capture_keeps_its_actual_type_and_colour():
    state = game("a7b8q", "c8b8", fen="1rr4k/P7/8/8/8/8/8/7K w - - 0 1")
    assert captured_pieces(state) == ((chess.Piece(chess.ROOK, chess.BLACK),), (chess.Piece(chess.QUEEN, chess.WHITE),))


def test_board_keeps_white_at_bottom_and_highlights_last_move_and_check(monkeypatch):
    state = game("f2f3", "e7e5", "g2g4", "d8h4")
    original = chess.svg.board
    options = []

    def recording(*args, **kwargs):
        options.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(chess_play_board.chess.svg, "board", recording)
    render_match(state)
    assert options[0]["orientation"] == chess.WHITE
    assert options[0]["lastmove"] == chess.Move.from_uci("d8h4")
    assert options[0]["check"] == chess.E1
    assert options[0]["colors"]["square light lastmove"] == "#f3da7b"


def test_final_banners_are_on_actual_images_and_timeout_changes_image_without_a_move(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = game("e2e4")
    active = render_match(state)
    state.expire(1700)
    timed_out = render_match(state)
    mated = render_match(game("f2f3", "e7e5", "g2g4", "d8h4"))
    assert active != timed_out != mated
    for data in (active, timed_out, mated):
        assert len(data) < 250_000
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "PNG" and image.size == (WIDTH, HEIGHT)
            assert image.convert("RGB").getpixel((0, 20)) == (21, 35, 46)
            image.load()
    with Image.open(io.BytesIO(active)) as live, Image.open(io.BytesIO(timed_out)) as final:
        assert live.crop((0, 0, WIDTH, 110)).tobytes() != final.crop((0, 0, WIDTH, 110)).tobytes()
        assert live.crop((24, 110, 744, 830)).tobytes() == final.crop((24, 110, 744, 830)).tobytes()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("reason,expected", [("timeout", "ВРЕМЯ ВЫШЛО"), ("checkmate", "МАТ")])
def test_svg_banner_contains_correct_winning_side(reason, expected):
    state = game()
    state.result = reason
    state.winner = 2
    texts = [node.text for node in ET.fromstring(chess_play_board._svg(state)).iter() if node.tag.endswith("text")]
    assert expected in texts
    assert "Победили чёрные" in texts
    assert "Белые взяли" in texts and "Чёрные взяли" in texts


def test_invalid_history_is_rejected():
    state = game()
    state.moves = ["e2e5"]
    with pytest.raises(ValueError):
        render_match(state)
