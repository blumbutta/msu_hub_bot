import io

import chess
import pytest
from PIL import Image

from msu_hub_bot.media.chessboard import BOARD_SIZE, render_board

FEN = "rkb2R2/p1p4p/1pB1p3/2n1q3/8/P1p5/1PP3PP/1K3R2 w - - 0 1"


def test_board_and_solution_render_as_bounded_png_without_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    question = render_board(FEN)
    solution = render_board(FEN, arrow="f8c8")
    assert question != solution
    for data in (question, solution):
        assert len(data) < 200_000
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "PNG" and image.size == (BOARD_SIZE, BOARD_SIZE)
            image.load()
    assert list(tmp_path.iterdir()) == []


def test_black_side_is_at_bottom_without_mirroring_files(monkeypatch):
    from msu_hub_bot.media import chessboard

    real_board = chess.svg.board
    captured = []

    def spy(*args, **kwargs):
        captured.append(kwargs)
        return real_board(*args, **kwargs)

    monkeypatch.setattr(chessboard.chess.svg, "board", spy)
    board = chess.Board(FEN)
    board.push_uci("f8c8")
    render_board(board.fen())
    assert captured[0]["orientation"] == chess.BLACK
    assert captured[0]["coordinates"] is True
    assert captured[0]["arrows"] == []


@pytest.mark.parametrize("fen,arrow", [("8/8/8/8/8/8/8/8 w - - 0 1", None), (FEN, "f8f1"), (FEN, "bad")])
def test_invalid_board_or_arrow_is_rejected(fen, arrow):
    with pytest.raises(ValueError):
        render_board(fen, arrow=arrow)
