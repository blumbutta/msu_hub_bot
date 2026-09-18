"""Serializable rules and clocks for a human-versus-human chess game."""

from typing import Literal, Self

import chess
from pydantic import BaseModel, ConfigDict, Field, model_validator

INITIAL_SECONDS = 600.0
INCREMENT_SECONDS = 5.0


class GameError(ValueError):
    """An expected, user-facing rejection of a game action."""


class Player(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0)
    name: str
    username: str | None = None


class Game(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    bot_id: int
    chat_id: int
    thread_id: int | None = None
    message_id: int | None = None
    white: Player
    black: Player | None = None
    white_rating: int = 800
    black_rating: int = 800
    created_at: float = Field(allow_inf_nan=False)
    invite_deadline: float = Field(allow_inf_nan=False)
    turn_started: float | None = Field(default=None, allow_inf_nan=False)
    white_seconds: float = Field(default=INITIAL_SECONDS, ge=0, allow_inf_nan=False)
    black_seconds: float = Field(default=INITIAL_SECONDS, ge=0, allow_inf_nan=False)
    moves: list[str] = Field(default_factory=list)
    initial_fen: str = chess.STARTING_FEN
    revision: int = Field(default=0, ge=0)
    selected: str | None = None
    draw_offer: int | None = None
    winner: int | None = None
    result: str | None = None

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        board = self.board()
        if self.invite_deadline < self.created_at:
            raise ValueError("Invitation ends before it was created")
        participants = {self.white.user_id}
        if self.black is not None:
            if self.black.user_id == self.white.user_id:
                raise ValueError("A player cannot occupy both sides")
            participants.add(self.black.user_id)
        if self.status == "playing":
            if self.turn_started is None or self.winner is not None:
                raise ValueError("Playing games require a running clock and no winner")
            if self.draw_offer is not None and self.draw_offer not in participants:
                raise ValueError("Only a participant can offer a draw")
            if self.selected is not None:
                origin = chess.parse_square(self.selected)
                if not any(move.from_square == origin for move in board.legal_moves):
                    raise ValueError("Selected piece must have a legal move")
        else:
            if self.turn_started is not None or self.selected is not None or self.draw_offer is not None:
                raise ValueError("Waiting and finished games cannot have an active turn")
            if self.status == "waiting" and (self.moves or self.winner is not None):
                raise ValueError("Waiting games cannot have moves or a winner")
        if self.status == "finished":
            if self.black is None:
                if self.result not in {"cancelled", "invite_expired"} or self.winner is not None or self.moves:
                    raise ValueError("Unplayed invitations cannot have a game result")
            elif self.result in {"checkmate", "timeout", "resigned"}:
                if self.winner not in participants:
                    raise ValueError("Decisive games require a participant winner")
            elif self.result in {
                "stalemate",
                "insufficient_material",
                "seventyfive_moves",
                "fivefold_repetition",
                "fifty_moves",
                "threefold_repetition",
                "agreed_draw",
            }:
                if self.winner is not None:
                    raise ValueError("Drawn games cannot have a winner")
            else:
                raise ValueError("Unknown game result")
        return self

    @property
    def status(self) -> Literal["waiting", "playing", "finished"]:
        if self.result is not None:
            return "finished"
        return "playing" if self.black is not None else "waiting"

    def board(self) -> chess.Board:
        # Full history retains castling, en passant and repetition information.
        board = chess.Board(self.initial_fen)
        if not board.is_valid():
            raise ValueError("Invalid starting chess position")
        for uci in self.moves:
            move = board.parse_uci(uci)
            if not move:
                raise ValueError("Null moves are not permitted")
            board.push(move)
        return board

    @property
    def turn_player(self) -> Player | None:
        if self.status != "playing":
            return None
        return self.white if self.board().turn == chess.WHITE else self.black

    def remaining(self, now: float) -> tuple[float, float]:
        white, black = self.white_seconds, self.black_seconds
        if self.status == "playing" and self.turn_started is not None:
            elapsed = max(0.0, now - self.turn_started)
            if self.board().turn == chess.WHITE:
                white = max(0.0, white - elapsed)
            else:
                black = max(0.0, black - elapsed)
        return white, black

    def deadline(self) -> float | None:
        if self.status == "finished":
            return None
        if self.status == "waiting":
            return self.invite_deadline
        assert self.turn_started is not None
        seconds = self.white_seconds if self.board().turn == chess.WHITE else self.black_seconds
        return self.turn_started + seconds

    def _finish(self, result: str, winner: int | None, now: float) -> None:
        self.white_seconds, self.black_seconds = self.remaining(now)
        self.result, self.winner = result, winner
        self.turn_started, self.selected, self.draw_offer = None, None, None
        self.revision += 1

    def expire(self, now: float) -> bool:
        deadline = self.deadline()
        if deadline is None or now < deadline:
            return False
        if self.status == "waiting":
            self._finish("invite_expired", None, now)
        else:
            assert self.black is not None
            # The requested clock rule is always a loss, including positions
            # where the opponent does not have sufficient mating material.
            winner = self.black if self.board().turn == chess.WHITE else self.white
            self._finish("timeout", winner.user_id, now)
        return True

    def _open(self, now: float) -> None:
        self.expire(now)
        if self.status == "finished":
            raise GameError("Партия уже завершена.")

    def _participant(self, user_id: int, now: float) -> None:
        self._open(now)
        if self.black is None:
            raise GameError("Ждём соперника.")
        if user_id not in (self.white.user_id, self.black.user_id):
            raise GameError("Вы наблюдаете за партией.")

    def _turn(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        player = self.turn_player
        assert player is not None
        if player.user_id != user_id:
            raise GameError("Сейчас ход соперника.")

    def join(self, player: Player, now: float) -> None:
        self._open(now)
        if self.black is not None:
            raise GameError("Место соперника уже занято.")
        if player.user_id == self.white.user_id:
            raise GameError("Нужен другой участник: вы играете белыми.")
        self.black, self.turn_started = player, now
        self.revision += 1

    def select(self, user_id: int, square: str | None, now: float) -> None:
        self._turn(user_id, now)
        if square is not None:
            try:
                origin = chess.parse_square(square)
            except ValueError as exc:
                raise GameError("Такой клетки нет.") from exc
            board = self.board()
            if not any(move.from_square == origin for move in board.legal_moves):
                raise GameError("Этой фигурой сейчас нельзя ходить.")
        if square != self.selected:
            self.selected = square
            self.revision += 1

    def move(self, user_id: int, uci: str, now: float) -> None:
        self._turn(user_id, now)
        board = self.board()
        try:
            move = board.parse_uci(uci)
        except ValueError as exc:
            raise GameError("Такой ход сейчас невозможен.") from exc
        if not move or move not in board.legal_moves:
            raise GameError("Такой ход сейчас невозможен.")
        white, black = self.remaining(now)
        if board.turn == chess.WHITE:
            white += INCREMENT_SECONDS
        else:
            black += INCREMENT_SECONDS
        board.push(move)
        self.white_seconds, self.black_seconds = white, black
        self.moves.append(move.uci())
        self.turn_started, self.selected = now, None
        if self.draw_offer != user_id:
            self.draw_offer = None
        outcome = board.outcome(claim_draw=False)
        if outcome is None:
            self.revision += 1
        else:
            assert self.black is not None
            winner = None if outcome.winner is None else self.white if outcome.winner == chess.WHITE else self.black
            self._finish(outcome.termination.name.lower(), winner.user_id if winner else None, now)

    def resign(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        assert self.black is not None
        winner = self.black if user_id == self.white.user_id else self.white
        self._finish("resigned", winner.user_id, now)

    def cancel(self, user_id: int, now: float) -> None:
        self._open(now)
        if self.status != "waiting" or user_id != self.white.user_id:
            raise GameError("Отменить приглашение может только его автор.")
        self._finish("cancelled", None, now)

    def offer_draw(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        if self.draw_offer is not None:
            raise GameError("Предложение ничьей уже отправлено.")
        self.draw_offer = user_id
        self.revision += 1

    def accept_draw(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        if self.draw_offer is None or self.draw_offer == user_id:
            raise GameError("Соперник не предлагал ничью.")
        self._finish("agreed_draw", None, now)

    def decline_draw(self, user_id: int, now: float) -> None:
        self._participant(user_id, now)
        if self.draw_offer is None or self.draw_offer == user_id:
            raise GameError("Соперник не предлагал ничью.")
        self.draw_offer = None
        self.revision += 1

    def claim_draw(self, user_id: int, now: float) -> None:
        self._turn(user_id, now)
        board = self.board()
        if board.can_claim_fifty_moves():
            self._finish("fifty_moves", None, now)
        elif board.can_claim_threefold_repetition():
            self._finish("threefold_repetition", None, now)
        else:
            raise GameError("Сейчас нельзя потребовать ничью по правилам.")
