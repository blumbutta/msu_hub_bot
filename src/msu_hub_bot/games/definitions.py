"""Question providers and renderers for the shared quiz lifecycle."""

import asyncio
import random
from dataclasses import dataclass
from typing import Literal

from aiogram.types import BufferedInputFile

from msu_hub_bot.commands import art_view, chess_view, geoguess_view, pokemon_view
from msu_hub_bot.commands.quiz_view import View
from msu_hub_bot.games.models import Question, RoundState, Vote
from msu_hub_bot.media.chessboard import render_board
from msu_hub_bot.media.pokemon import render_pokemon
from msu_hub_bot.providers.art import Artwork, download_artwork, random_artwork
from msu_hub_bot.providers.chess import MoveOption, Puzzle, random_puzzle
from msu_hub_bot.providers.geoguess import COUNTRIES, Photo, random_photo
from msu_hub_bot.providers.pokemon import Pokemon, download_pokemon, random_pokemon


def chess_puzzle(question: Question) -> Puzzle:
    if question.fen is None or not question.solution or len(question.moves) != 6:
        raise ValueError("Incomplete chess question")
    return Puzzle(
        id=question.identity,
        fen=question.fen,
        solution=tuple(question.solution),
        options=tuple(MoveOption(move, label) for move, label in zip(question.moves, question.choices, strict=True)),
        line=tuple(question.line),
    )


def geoguess_photo(question: Question) -> Photo:
    values = (question.country, question.city, question.url, question.source, question.author, question.license, question.license_url)
    if any(value is None for value in values):
        raise ValueError("Incomplete geography question")
    return Photo(*(value or "" for value in values))


def artwork(question: Question) -> Artwork:
    if not question.artwork_title or not question.author or not question.url or not question.source:
        raise ValueError("Incomplete art question")
    return Artwork(
        id=question.identity,
        title=question.artwork_title,
        artist=question.author,
        date=question.artwork_date or "",
        image_url=question.url,
        source_url=question.source,
    )


def pokemon(question: Question) -> Pokemon:
    if not question.pokemon_name or not question.url or not question.source:
        raise ValueError("Incomplete pokemon question")
    return Pokemon(id=question.identity, name=question.pokemon_name, image_url=question.url, source_url=question.source)


@dataclass(frozen=True)
class Definition:
    name: Literal["chess", "geoguess", "art", "pokemon"]
    photo_timeout: float | None = None
    reveal_photo: bool = False
    reveal_timeout: float = 5

    async def load(self, recent: list[str]) -> Question:
        if self.name == "pokemon":
            pokemon_puzzle = await random_pokemon(tuple(recent))
            selected = pokemon_puzzle.pokemon
            return Question(
                kind="pokemon",
                identity=selected.id,
                choices=list(pokemon_puzzle.options),
                answer=pokemon_puzzle.answer,
                pokemon_name=selected.name,
                url=selected.image_url,
                source=selected.source_url,
            )
        if self.name == "art":
            art_puzzle = await random_artwork(tuple(recent))
            painting = art_puzzle.artwork
            return Question(
                kind="art",
                identity=painting.id,
                choices=list(art_puzzle.options),
                answer=art_puzzle.answer,
                artwork_title=painting.title,
                artwork_date=painting.date,
                author=painting.artist,
                url=painting.image_url,
                source=painting.source_url,
            )
        if self.name == "chess":
            puzzle = await random_puzzle(tuple(recent))
            return Question(
                kind="chess",
                identity=puzzle.id,
                choices=[option.label for option in puzzle.options],
                answer=next(index for index, option in enumerate(puzzle.options) if option.uci == puzzle.solution[0]),
                fen=puzzle.fen,
                solution=list(puzzle.solution),
                line=list(puzzle.line),
                moves=[option.uci for option in puzzle.options],
            )
        photo = await random_photo(tuple(recent))
        options = random.sample(sorted(set(COUNTRIES.values()) - {photo.country}), 5) + [photo.country]
        random.shuffle(options)
        return Question(
            kind="geoguess",
            identity=photo.country,
            choices=options,
            answer=options.index(photo.country),
            country=photo.country,
            city=photo.city,
            url=photo.url,
            source=photo.source,
            author=photo.author,
            license=photo.license,
            license_url=photo.license_url,
        )

    async def photo(self, question: Question, *, solution: bool = False) -> BufferedInputFile | str:
        if self.name == "pokemon":
            source = await download_pokemon(pokemon(question).image_url)
            image = await asyncio.to_thread(render_pokemon, source, solution=solution)
            return BufferedInputFile(image, filename="pokemon-solution.png" if solution else "pokemon.png")
        if self.name == "art":
            return BufferedInputFile(await download_artwork(artwork(question).image_url), filename="art.jpg")
        if self.name == "geoguess":
            if question.url is None:
                raise ValueError("Missing geography photo")
            return question.url
        puzzle = chess_puzzle(question)
        image = await asyncio.to_thread(render_board, puzzle.fen, arrow=puzzle.solution[0] if solution else None)
        return BufferedInputFile(image, filename="chess-solution.png" if solution else "chess.png")

    def label(self, question: Question, choice: int) -> str:
        label = question.choices[choice]
        return geoguess_view.country_label(label) if self.name == "geoguess" else label

    def render(self, state: RoundState, votes: list[Vote]) -> View:
        question = state.question
        if question is None:
            raise ValueError("Missing quiz question")
        closed = state.phase == "closed"
        scored = None if state.score_status == "pending" else state.score_status == "recorded"
        if self.name == "pokemon":
            pokemon_players = [
                pokemon_view.Player(vote.user_id, vote.name, vote.username, question.choices[vote.choice], vote.choice == question.answer)
                for vote in votes
            ]
            return pokemon_view.render(pokemon(question), pokemon_players, closed=closed, scored=scored, page=state.page)
        if self.name == "art":
            art_players = [
                art_view.Player(vote.user_id, vote.name, vote.username, question.choices[vote.choice], vote.choice == question.answer)
                for vote in votes
            ]
            return art_view.render(artwork(question), art_players, closed=closed, scored=scored, page=state.page)
        if self.name == "chess":
            chess_players = [
                chess_view.Player(vote.user_id, vote.name, vote.username, question.choices[vote.choice], vote.choice == question.answer)
                for vote in votes
            ]
            return chess_view.render(chess_puzzle(question), chess_players, closed=closed, scored=scored, page=state.page)
        geo_players = [
            geoguess_view.Player(vote.user_id, vote.name, vote.username, question.choices[vote.choice], vote.choice == question.answer)
            for vote in votes
        ]
        return geoguess_view.render(geoguess_photo(question), geo_players, closed=closed, scored=scored, page=state.page)


DEFINITIONS = {name: Definition(name, reveal_photo=name == "chess") for name in ("chess", "geoguess")}
DEFINITIONS["art"] = Definition("art", photo_timeout=20)
DEFINITIONS["pokemon"] = Definition("pokemon", photo_timeout=20, reveal_photo=True, reveal_timeout=10)
