"""Render a validated chess position without files, system fonts or an engine."""

import chess
import chess.svg
import resvg_py

BOARD_SIZE = 720


def render_board(fen: str, *, arrow: str | None = None) -> bytes:
    board = chess.Board(fen)
    if not board.is_valid():
        raise ValueError("Invalid chess position")
    arrows: list[chess.svg.Arrow] = []
    if arrow is not None:
        move = chess.Move.from_uci(arrow)
        if move not in board.legal_moves:
            raise ValueError("Illegal solution move")
        arrows.append(chess.svg.Arrow(move.from_square, move.to_square, color="#168a45cc"))
    # python-chess supplies vector pieces and coordinate outlines; no font lookup.
    svg = chess.svg.board(
        board,
        orientation=board.turn,
        size=BOARD_SIZE,
        coordinates=True,
        check=board.king(board.turn) if board.is_check() else None,
        arrows=arrows,
        colors={"square light": "#eee6d4", "square dark": "#8ca37a", "margin": "#252b30", "coord": "#f5f2e8"},
    )
    return resvg_py.svg_to_bytes(svg_string=svg, skip_system_fonts=True)
