"""Shared game cards, bounded captions and legal two-step move choices."""

import chess
import pytest
from aiogram.utils.formatting import Text

from msu_hub_bot.commands.chess_play_game import Game, Player
from msu_hub_bot.commands.chess_play_view import PlayCallback, clock, keyboard, render
from msu_hub_bot.commands.quiz_view import CAPTION_LIMIT


def game(*, playing=True, **values):
    result = Game(
        token="012345abcdef",
        bot_id=42,
        chat_id=-1001234567890,
        white=Player(user_id=1, name="Белый <b>&", username="white"),
        created_at=1000,
        invite_deadline=1600,
        **values,
    )
    if playing:
        result.join(Player(user_id=2, name="Чёрный", username="black"), 1100)
    return result


def buttons(state):
    markup = keyboard(state)
    assert markup is not None
    return [button for row in markup.inline_keyboard for button in row]


def callbacks(state, action):
    return [
        PlayCallback.unpack(button.callback_data)
        for button in buttons(state)
        if button.callback_data and PlayCallback.unpack(button.callback_data).action == action
    ]


def validate_caption(view):
    encoded = view.caption.encode("utf-16-le")
    assert 0 < len(encoded) // 2 <= CAPTION_LIMIT
    assert view.page == 0 and view.pages == 1
    for entity in view.entities:
        assert 0 <= entity.offset < len(encoded) // 2
        assert entity.length > 0 and entity.offset + entity.length <= len(encoded) // 2
        assert encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
    assert Text.from_entities(view.caption, view.entities).render()[0] == view.caption


def test_invitation_labels_creator_and_defers_clocks_until_join():
    state = game(playing=False)
    view = render(state, 1150)
    assert "Белый <b>& (@white) — 10:00" in view.caption
    assert "Ждём соперника — 10:00" in view.caption
    assert "Часы начнут идти, когда соперник присоединится" in view.caption
    assert [button.text for button in buttons(state)] == ["Сыграть против @white", "Отменить приглашение"]
    assert [PlayCallback.unpack(button.callback_data).action for button in buttons(state)] == ["join", "cancel"]
    assert callbacks(state, "join")[0].game == state.token
    assert callbacks(state, "join")[0].revision == state.revision
    validate_caption(view)


def test_invitation_uses_name_when_username_is_missing():
    state = game(playing=False)
    state.white.username = None
    assert buttons(state)[0].text == "Сыграть против Белый <b>&"


def test_choosing_piece_then_destination_only_exposes_legal_moves():
    state = game()
    origins = callbacks(state, "pick")
    assert len(origins) == 10
    assert {value.value for value in origins} == {chess.square_name(move.from_square) for move in state.board().legal_moves}
    assert not callbacks(state, "move")
    assert "♘ g1" in [button.text for button in buttons(state)]
    state.select(1, "e2", 1100)
    assert {value.value for value in callbacks(state, "move")} == {"e2e3", "e2e4"}
    assert {button.text for button in buttons(state)} >= {"e3", "e4", "← Назад"}
    assert not callbacks(state, "pick")
    assert all(value.revision == state.revision for value in callbacks(state, "move"))
    state.select(1, "g1", 1100)
    assert {value.value for value in callbacks(state, "move")} == {"g1h3", "g1f3"}


def test_pinned_piece_does_not_offer_an_illegal_destination():
    state = game(initial_fen="4r1k1/8/8/8/8/8/4R3/4K3 w - - 0 1")
    state.select(1, "e2", 1100)
    assert {value.value for value in callbacks(state, "move")} == {f"e2e{rank}" for rank in range(3, 9)}


def test_every_promotion_has_an_explicit_piece_choice_and_full_uci():
    state = game(initial_fen="1r5k/P7/8/8/8/8/8/7K w - - 0 1")
    state.select(1, "a7", 1100)
    options = callbacks(state, "move")
    assert {value.value for value in options} == {f"a7{destination}{piece}" for destination in ("a8", "b8") for piece in "qrbn"}
    assert [button.text for button in buttons(state)][:4] == ["a8 → ♕", "a8 → ♖", "a8 → ♗", "a8 → ♘"]
    assert all(len(button.callback_data.encode()) <= 64 for button in buttons(state))


def test_castling_is_labelled_and_keeps_the_standard_uci_move():
    state = game(initial_fen="r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    state.select(1, "e1", 1100)
    labelled = {button.text: PlayCallback.unpack(button.callback_data).value for button in buttons(state)}
    assert labelled["♔ c1 (O-O-O)"] == "e1c1"
    assert labelled["♔ g1 (O-O)"] == "e1g1"


def test_draw_offer_has_accept_decline_and_claim_only_when_available():
    state = game()
    assert callbacks(state, "draw")
    assert not callbacks(state, "accept_draw")
    assert not callbacks(state, "claim_draw")
    state.offer_draw(1, 1100)
    assert not callbacks(state, "draw")
    assert callbacks(state, "accept_draw") and callbacks(state, "decline_draw")
    assert "Белый <b>& (@white) предлагает ничью" in render(state, 1100).caption
    for uci in ["g1f3", "g8f6", "f3g1", "f6g8"] * 2:
        state.move(state.turn_player.user_id, uci, 1100)
    assert callbacks(state, "claim_draw")


@pytest.mark.parametrize("seconds,expected", [(600, "10:00"), (605, "10:05"), (0.1, "00:01"), (0, "00:00"), (-1, "00:00")])
def test_clock_snapshot_does_not_show_zero_early(seconds, expected):
    assert clock(seconds) == expected


def test_card_shows_both_clocks_turn_and_increment_after_a_move():
    state = game()
    state.move(1, "e2e4", 1110)
    view = render(state, 1117)
    assert "Белый <b>& (@white) — 09:55" in view.caption
    assert "Чёрный (@black) — 09:53" in view.caption
    assert "Ход чёрных: Чёрный (@black)" in view.caption
    assert "Последний ход: Пешка e2 → e4" in view.caption
    assert "За сделанный ход +5 секунд" in view.caption
    assert "Часы обновляются каждые 5 секунд" in view.caption
    validate_caption(view)


def test_capture_last_move_also_uses_an_arrow():
    state = game()
    for uid, uci in [(1, "e2e4"), (2, "d7d5"), (1, "e4d5")]:
        state.move(uid, uci, 1100)
    caption = render(state, 1100).caption
    assert "Пешка e4 → d5" in caption
    assert "×" not in caption


def test_finished_games_show_winner_reason_frozen_clocks_and_no_buttons():
    state = game()
    state.expire(1700)
    view = render(state, 9900)
    assert "🏆 Победитель: Чёрный (@black)" in view.caption
    assert "Время вышло" in view.caption
    assert "Белый <b>& (@white) — 00:00" in view.caption
    assert "Чёрный (@black) — 10:00" in view.caption
    assert "Ход белых" not in view.caption
    assert keyboard(state) is None
    validate_caption(view)


@pytest.mark.parametrize("reason,expected", [("agreed_draw", "По соглашению"), ("stalemate", "Пат"), ("threefold_repetition", "три раза")])
def test_draw_result_has_reason_without_a_winner(reason, expected):
    state = game()
    state.result = reason
    view = render(state, 1100)
    assert "🤝 Ничья" in view.caption and expected in view.caption
    assert "Победитель" not in view.caption
    assert keyboard(state) is None
    validate_caption(view)


def test_cancelled_invitation_is_not_mislabelled_a_draw():
    state = game(playing=False)
    state.cancel(1, 1150)
    view = render(state, 1150)
    assert "Партия не началась" in view.caption and "Приглашение отменено" in view.caption
    assert "Ничья" not in view.caption
    assert keyboard(state) is None


def test_hostile_long_emoji_names_are_bounded_and_only_mentions_are_links():
    state = game()
    state.white.name = '<a href="https://invalid.test">🧑🏽‍🚀&</a>\n' * 100
    state.white.username = "long" * 100
    state.black.name = "🧑🏽‍🚀" * 100
    state.black.username = "other" * 100
    state.offer_draw(2, 1100)
    state.select(1, "e2", 1100)
    for current in (state, state.model_copy(update={"result": "checkmate", "winner": 1})):
        view = render(current, 1100)
        validate_caption(view)
        assert all(not entity.url or entity.url in ("tg://user?id=1", "tg://user?id=2") for entity in view.entities)
    waiting = state.model_copy(update={"black": None, "result": None})
    validate_caption(render(waiting, 1100))
    assert len(Text(buttons(waiting)[0].text)) < 64


def test_active_game_shows_global_rating_snapshots():
    state = game(white_rating=872, black_rating=934)
    view = render(state, 1100)
    assert "Белый <b>& (@white) — 10:00 • Elo 872" in view.caption
    assert "Чёрный (@black) — 10:00 • Elo 934" in view.caption
    assert "Рейтинг обновляется" not in view.caption
    validate_caption(view)


def test_finished_ratings_use_actual_transaction_values_not_starting_snapshots():
    state = game(white_rating=800, black_rating=800)
    state.resign(2, 1100)
    pending = render(state, 1100)
    assert "Рейтинг обновляется…" in pending.caption
    view = render(state, 1100, ratings=((816, 831), (780, 765)))
    assert "Elo 816 → 831 (+15)" in view.caption
    assert "Elo 780 → 765 (-15)" in view.caption
    assert "Рейтинг обновляется" not in view.caption
    assert "Победитель: Белый <b>& (@white)" in view.caption
    assert "Соперник сдался" in view.caption
    validate_caption(view)


def test_cancelled_invitation_has_no_pending_rating_and_never_counts_as_a_game():
    state = game(playing=False)
    state.expire(1700)
    assert "Рейтинг обновляется" not in render(state, 1800).caption


def test_piece_buttons_have_only_coloured_symbol_and_current_square():
    state = game()
    white = {button.text for button in buttons(state) if PlayCallback.unpack(button.callback_data).action == "pick"}
    assert white == {"♘ b1", "♘ g1", *(f"♙ {file}2" for file in "abcdefgh")}
    state.move(1, "e2e4", 1100)
    black = {button.text for button in buttons(state) if PlayCallback.unpack(button.callback_data).action == "pick"}
    assert black == {"♞ b8", "♞ g8", *(f"♟ {file}7" for file in "abcdefgh")}


def test_black_promotion_buttons_use_black_piece_symbols():
    state = game(initial_fen="7k/8/8/8/8/8/p7/7K b - - 0 1")
    state.select(2, "a2", 1100)
    assert [button.text for button in buttons(state)][:4] == ["a1 → ♛", "a1 → ♜", "a1 → ♝", "a1 → ♞"]
    assert {value.value for value in callbacks(state, "move")} == {f"a2a1{piece}" for piece in "qrbn"}
