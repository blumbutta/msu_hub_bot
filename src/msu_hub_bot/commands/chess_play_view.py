"""Shared chess-board captions and a two-step legal-move keyboard."""

import math

import chess
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.formatting import Bold, Text

from msu_hub_bot.commands.chess_play_game import Game, Player
from msu_hub_bot.commands.quiz_view import View, compact, user_label

PIECE_NAMES = {
    chess.PAWN: "Пешка",
    chess.KNIGHT: "Конь",
    chess.BISHOP: "Слон",
    chess.ROOK: "Ладья",
    chess.QUEEN: "Ферзь",
    chess.KING: "Король",
}
PROMOTION_ORDER = {chess.QUEEN: 0, chess.ROOK: 1, chess.BISHOP: 2, chess.KNIGHT: 3}
RESULT_LABELS = {
    "checkmate": "Мат.",
    "timeout": "Время вышло.",
    "resigned": "Соперник сдался.",
    "stalemate": "Пат.",
    "insufficient_material": "Недостаточно материала для мата.",
    "seventyfive_moves": "75 ходов без взятия и хода пешкой.",
    "fivefold_repetition": "Позиция повторилась пять раз.",
    "fifty_moves": "50 ходов без взятия и хода пешкой.",
    "threefold_repetition": "Позиция повторилась три раза.",
    "agreed_draw": "По соглашению игроков.",
    "cancelled": "Приглашение отменено.",
    "invite_expired": "Никто не присоединился за 10 минут.",
}


class PlayCallback(CallbackData, prefix="chplay"):
    game: str
    revision: int
    action: str
    value: str = ""


def _player(player: Player) -> Text:
    return user_label(player.user_id, player.name, player.username)


def clock(seconds: float) -> str:
    """Round up a running clock so it does not show zero before flag fall."""
    value = max(0, math.ceil(seconds))
    minutes, seconds = divmod(value, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _move_label(board: chess.Board, move: chess.Move) -> str:
    piece = board.piece_at(move.from_square)
    assert piece is not None
    name = "Рокировка" if board.is_castling(move) else PIECE_NAMES[piece.piece_type]
    label = f"{name} {chess.square_name(move.from_square)} → {chess.square_name(move.to_square)}"
    if move.promotion is not None:
        label += f" = {PIECE_NAMES[move.promotion]}"
    return label


def render(game: Game, now: float, *, ratings: tuple[tuple[int, int], tuple[int, int]] | None = None) -> View:
    """Keep the two players, both clocks and the current action in one caption."""
    board = game.board()
    white_seconds, black_seconds = game.remaining(now)

    def rating_label(index: int, initial: int) -> str:
        if game.status == "finished" and ratings is not None:
            before, after = ratings[index]
            return f" • Elo {before} → {after} ({after - before:+d})"
        return f" • Elo {initial}"

    rows: list[Text] = [
        Text(Bold("♟ Шахматы • 10 + 5")),
        Text("⚪ ", _player(game.white), f" — {clock(white_seconds)}{rating_label(0, game.white_rating)}"),
        Text("⚫ ", _player(game.black), f" — {clock(black_seconds)}{rating_label(1, game.black_rating)}")
        if game.black
        else Text("⚫ Ждём соперника — 10:00"),
    ]
    if game.status == "waiting":
        rows.extend(
            (
                Text("\nПервый участник, нажавший кнопку, играет чёрными."),
                Text("Приглашение действует 10 минут. Часы начнут идти, когда соперник присоединится."),
            )
        )
    elif game.status == "finished":
        if game.winner is not None:
            winner = game.white if game.winner == game.white.user_id else game.black
            assert winner is not None
            rows.append(Text("\n🏆 Победитель: ", _player(winner), "."))
        elif game.black is not None:
            rows.append(Text("\n🤝 Ничья."))
        else:
            rows.append(Text("\nПартия не началась."))
        rows.append(Text(RESULT_LABELS.get(game.result or "", compact(game.result or "Партия завершена.", 120))))
        if game.black is not None and ratings is None and game.result not in ("cancelled", "invite_expired"):
            rows.append(Text("Рейтинг обновляется…"))
    else:
        turn = game.white if board.turn == chess.WHITE else game.black
        assert turn is not None
        rows.append(Text("\nХод ", "белых: " if board.turn else "чёрных: ", _player(turn), "."))
        if board.is_check():
            rows.append(Text(Bold("Шах!")))
        if game.selected:
            rows.append(Text("Выбрана фигура на ", game.selected, ". Выбери клетку назначения или нажми «Назад»."))
        else:
            rows.append(Text("Выбери фигуру, затем клетку назначения."))
        if game.draw_offer is not None:
            offerer = game.white if game.draw_offer == game.white.user_id else game.black
            assert offerer is not None
            rows.append(Text(_player(offerer), " предлагает ничью."))
        rows.append(Text("За сделанный ход +5 секунд. Часы обновляются каждые 5 секунд."))
    if board.move_stack:
        previous = board.copy(stack=1)
        last_move = previous.pop()
        rows.append(Text("Последний ход: ", _move_label(previous, last_move), "."))
    text = Text(*[Text(row, "\n" if index + 1 < len(rows) else "") for index, row in enumerate(rows)])
    caption, entities = text.render()
    return View(caption, entities, 0, 1)


def keyboard(game: Game) -> InlineKeyboardMarkup | None:
    """Only show playable pieces, then legal destinations for the selected piece."""
    if game.status == "finished":
        return None

    def button(label: str, action: str, value: str = "") -> InlineKeyboardButton:
        callback = PlayCallback(game=game.token, revision=game.revision, action=action, value=value)
        return InlineKeyboardButton(text=label, callback_data=callback.pack())

    if game.status == "waiting":
        opponent = f"@{game.white.username.lstrip('@')}" if game.white.username else game.white.name
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [button(f"Сыграть против {compact(opponent, 36) or 'игрока'}", "join")],
                [button("Отменить приглашение", "cancel")],
            ]
        )
    board = game.board()
    legal = list(board.legal_moves)
    moves = [move for move in legal if chess.square_name(move.from_square) == game.selected] if game.selected else []
    choices: list[InlineKeyboardButton] = []
    if moves:
        for move in sorted(moves, key=lambda move: (move.to_square, PROMOTION_ORDER.get(move.promotion or 0, 0))):
            destination = chess.square_name(move.to_square)
            if move.promotion is not None:
                label = f"{destination} → {chess.Piece(move.promotion, board.turn).unicode_symbol()}"
            elif board.is_castling(move):
                label = f"{chess.Piece(chess.KING, board.turn).unicode_symbol()} {destination} (O-O{'-O' if board.is_queenside_castling(move) else ''})"
            else:
                label = destination
            choices.append(button(label, "move", move.uci()))
    else:
        for square in sorted({move.from_square for move in legal}):
            piece = board.piece_at(square)
            assert piece is not None
            name = chess.square_name(square)
            choices.append(button(f"{piece.unicode_symbol()} {name}", "pick", name))
    columns = 2 if any(move.promotion for move in moves) else 3
    rows = [choices[index : index + columns] for index in range(0, len(choices), columns)]
    if moves:
        rows.append([button("← Назад", "back")])
    if game.draw_offer is not None:
        rows.append([button("Принять ничью", "accept_draw"), button("Отклонить ничью", "decline_draw")])
    else:
        rows.append([button("Предложить ничью", "draw")])
    if board.can_claim_draw():
        rows.append([button("Заявить ничью по правилам", "claim_draw")])
    rows.append([button("Сдаться", "resign")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
