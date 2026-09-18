"""A shared chess match board with captured pieces and visible final results."""

import xml.etree.ElementTree as ET
from html import escape

import chess
import chess.svg
import resvg_py

from msu_hub_bot import resources
from msu_hub_bot.commands.chess_play_game import Game

WIDTH = 768
HEIGHT = 970
BOARD_SIZE = 720
BOARD_X = 24
BOARD_Y = 110
FONT = "Liberation Sans"


def _position(game: Game) -> tuple[chess.Board, list[chess.Piece], list[chess.Piece]]:
    board = chess.Board(game.initial_fen)
    if not board.is_valid():
        raise ValueError("Invalid chess position")
    white: list[chess.Piece] = []
    black: list[chess.Piece] = []
    for uci in game.moves:
        move = board.parse_uci(uci)
        if not move:
            raise ValueError("Null moves are not allowed")
        captured = chess.Piece(chess.PAWN, not board.turn) if board.is_en_passant(move) else board.piece_at(move.to_square)
        if captured is not None:
            (white if board.turn == chess.WHITE else black).append(captured)
        board.push(move)
    return board, white, black


def captured_pieces(game: Game) -> tuple[tuple[chess.Piece, ...], tuple[chess.Piece, ...]]:
    """Return pieces taken by white and black, preserving promotions and en passant."""
    _, white, black = _position(game)
    return tuple(white), tuple(black)


def _text(value: str, x: int, y: int, size: int, fill: str, *, anchor: str = "start") -> str:
    return f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" fill="{fill}" text-anchor="{anchor}">{escape(value)}</text>'


def _banner(game: Game, board: chess.Board) -> tuple[str, str, str]:
    if game.status == "waiting":
        return "ШАХМАТЫ · 10 + 5", "Ожидаем соперника", "#b4d7cc"
    if game.status == "finished":
        title = {
            "timeout": "ВРЕМЯ ВЫШЛО",
            "checkmate": "МАТ",
            "resigned": "СДАЧА ПАРТИИ",
            "cancelled": "ПРИГЛАШЕНИЕ ОТМЕНЕНО",
            "invite_expired": "ПРИГЛАШЕНИЕ ЗАВЕРШЕНО",
        }.get(game.result or "", "НИЧЬЯ")
        if game.winner is not None:
            subtitle = "Победили белые" if game.winner == game.white.user_id else "Победили чёрные"
        else:
            subtitle = "Партия не началась" if game.black is None else "Партия завершена"
        return title, subtitle, "#f2c66d"
    side = "БЕЛЫХ" if board.turn == chess.WHITE else "ЧЁРНЫХ"
    title = f"ШАХ · ХОД {side}" if board.is_check() else f"ХОД {side}"
    return title, "Последний ход выделен золотым", "#eea8a0" if board.is_check() else "#b4d7cc"


def _svg(game: Game) -> str:
    board, white, black = _position(game)
    last_move = board.peek() if board.move_stack else None
    board_svg = ET.fromstring(
        chess.svg.board(
            board,
            orientation=chess.WHITE,
            size=BOARD_SIZE,
            coordinates=True,
            lastmove=last_move,
            check=board.king(board.turn) if board.is_check() else None,
            colors={
                "square light": "#eee6d4",
                "square dark": "#8ca37a",
                "square light lastmove": "#f3da7b",
                "square dark lastmove": "#d3b451",
                "margin": "#25343e",
                "coord": "#f5f2e8",
            },
        )
    )
    board_svg.set("x", str(BOARD_X))
    board_svg.set("y", str(BOARD_Y))
    title, subtitle, accent = _banner(game, board)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#15232e"/>',
        f'<rect width="{WIDTH}" height="6" fill="{accent}"/>',
        _text(title, WIDTH // 2, 52, 30, accent, anchor="middle"),
        _text(subtitle, WIDTH // 2, 86, 20, "#e8eee9", anchor="middle"),
        ET.tostring(board_svg, encoding="unicode"),
    ]
    for index, (label, pieces) in enumerate((("Белые взяли", white), ("Чёрные взяли", black))):
        y = 844 + 54 * index
        parts.append(f'<rect x="24" y="{y}" width="720" height="46" rx="9" fill="#dce2dd"/>')
        parts.append(_text(label, 38, y + 29, 18, "#293b45"))
        if not pieces:
            parts.append(_text("—", 202, y + 29, 20, "#53645b"))
        for offset, piece in enumerate(pieces):
            piece_svg = ET.fromstring(chess.svg.piece(piece, size=34))
            piece_svg.set("x", str(194 + offset * 34))
            piece_svg.set("y", str(y + 6))
            parts.append(ET.tostring(piece_svg, encoding="unicode"))
    parts.append("</svg>")
    return "".join(parts)


def render_match(game: Game) -> bytes:
    """Render bounded PNG bytes using only bundled fonts and vector chess pieces."""
    return resvg_py.svg_to_bytes(svg_string=_svg(game), skip_system_fonts=True, font_files=[str(resources.meme_font)])
