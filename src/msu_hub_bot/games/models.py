"""JSON records for chat quizzes; Python tasks and Telegram objects stay outside."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from msu_hub_bot.storage.features import Payload


class Question(Payload):
    """Freeze the question and answer order before publishing its buttons."""

    kind: Literal["chess", "geoguess", "art"]
    identity: str
    artwork_title: str | None = None
    artwork_date: str | None = None
    choices: list[str] = Field(min_length=6, max_length=6)
    answer: int = Field(ge=0, lt=6)
    fen: str | None = None
    solution: list[str] = Field(default_factory=list)
    line: list[str] = Field(default_factory=list)
    moves: list[str] = Field(default_factory=list)
    country: str | None = None
    city: str | None = None
    url: str | None = None
    source: str | None = None
    author: str | None = None
    license: str | None = None
    license_url: str | None = None


class ChatState(Payload):
    active: str | None = None
    recent: list[str] = Field(default_factory=list, max_length=15)


class RoundState(Payload):
    token: str
    chat_id: int
    thread_id: int | None = None
    question: Question | None = None
    phase: Literal["preparing", "publishing", "active", "closed", "abandoned"] = "preparing"
    message_id: int | None = None
    prepared_at: AwareDatetime
    published_at: AwareDatetime | None = None
    deadline_at: AwareDatetime | None = None
    closed_at: AwareDatetime | None = None
    score_day: date | None = None
    score_status: Literal["pending", "recorded", "skipped"] = "pending"
    score_cursor: str | None = None
    score_count: int = Field(default=0, ge=0)
    vote_count: int = Field(default=0, ge=0)
    page: int = Field(default=0, ge=0)


class Vote(Payload):
    user_id: int
    choice: int = Field(ge=0, lt=6)
    name: str = Field(max_length=256)
    username: str | None = Field(default=None, max_length=64)
    accepted_at: AwareDatetime


class Score(Payload):
    """A player's daily points and most recently scored display name."""

    user_id: int
    points: int = Field(ge=0)
    name: str = Field(max_length=256)
    username: str | None = Field(default=None, max_length=64)
