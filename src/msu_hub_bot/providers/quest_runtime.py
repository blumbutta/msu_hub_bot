"""A deliberately small, declarative subset of the Meander archive format.

No source from a book is executed. Unsupported story logic is rejected before
play starts, so a chat cannot get halfway through an incompatible story.
"""

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import JsonValue

from msu_hub_bot.providers.quest import QuestError, QuestScene

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 96 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_NODES = 2000
MAX_ITEMS = 100
MAX_CHOICES = 20
MAX_SCENE_TEXT = 16_000
MAX_SCENE_BYTES = 24 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_STEPS = 2000
MAX_REDIRECTS = 32

_CONFIG_KEYS = {
    "id",
    "title",
    "description",
    "author",
    "startNodeId",
    "password",
    "created",
    "lastOpened",
    "version",
    "updated",
    "backgroundAssetId",
    "dimBackground",
    "hideNodeTitles",
    "nodeTransitionMode",
    "scriptEngineMode",
    "pluginDependencies",
    "tags",
    "nodes",
    "connections",
    "variables",
    "customFontFileName",
    "screenshot_files",
    "genres",
    "category",
    "wrapButtonText",
    "defaultTextFit",
    "allowOpenContent",
    "previewFileName",
    "__folders__",
}
_NODE_KEYS = {"id", "chapterId", "title", "content", "x", "y", "color", "backgroundAudioVolume", "backgroundAssetId", "itemSpacing"}
_ITEM_KEYS = {
    "id",
    "type",
    "text",
    "resourcePath",
    "targetNodeId",
    "scriptTriggers",
    "isHidden",
    "flex",
    "crossAxisAlignment",
    "rowTextAlignment",
    "textFit",
    "wrapText",
    "volume",
    "isIncoming",
    "animateIn",
}


def _unsupported(detail: str) -> QuestError:
    return QuestError(f"Этот квест пока не поддерживается в чате: {detail}.")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise QuestError("Некорректный формат квеста.")
    return cast(dict[str, object], value)


def _list(value: object, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise QuestError("Некорректный или слишком большой список в квесте.")
    return cast(list[object], value)


def _text(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise QuestError("Некорректный текст в квесте.")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise QuestError("Недопустимые символы в тексте квеста.")
    return value


def _keys(value: dict[str, object], allowed: set[str]) -> None:
    if value.keys() - allowed:
        raise _unsupported("неизвестные параметры сценария")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QuestError("Повторяющиеся поля в квесте.")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise QuestError("Некорректное число в квесте.")


@dataclass(frozen=True)
class _Choice:
    label: str
    target: str


@dataclass(frozen=True)
class _Node:
    text: str
    choices: tuple[_Choice, ...]
    image_path: str | None
    redirect: str | None


@dataclass(frozen=True)
class MndBook:
    id: str
    title: str
    author: str
    digest: str
    _start: str
    _nodes: dict[str, _Node]
    _images: dict[str, bytes]

    @property
    def image_bytes(self) -> int:
        return sum(len(value) for value in self._images.values())

    def _resolve(self, node_id: str) -> str:
        seen: set[str] = set()
        for _ in range(MAX_REDIRECTS):
            if node_id in seen:
                raise QuestError("В квесте обнаружен бесконечный переход.")
            seen.add(node_id)
            node = self._nodes.get(node_id)
            if node is None:
                raise QuestError("В квесте отсутствует сцена перехода.")
            if node.redirect is None:
                return node_id
            node_id = node.redirect
        raise QuestError("В квесте слишком много автоматических переходов.")

    def _state(self, state: dict[str, JsonValue]) -> tuple[str, int]:
        if set(state) != {"node", "steps"}:
            raise QuestError("Сохранение квеста повреждено.")
        node_id, steps = state.get("node"), state.get("steps")
        if not isinstance(node_id, str) or node_id not in self._nodes or type(steps) is not int or not 0 <= steps <= MAX_STEPS:
            raise QuestError("Сохранение квеста повреждено.")
        if self._resolve(node_id) != node_id:
            raise QuestError("Сохранение содержит незавершённый переход.")
        return node_id, steps

    def start(self) -> dict[str, JsonValue]:
        return {"node": self._resolve(self._start), "steps": 0}

    def view(self, state: dict[str, JsonValue]) -> QuestScene:
        node_id, _ = self._state(state)
        node = self._nodes[node_id]
        image = self._images[node.image_path] if node.image_path is not None else None
        return QuestScene(node.text, tuple(choice.label for choice in node.choices), image)

    def choose(self, state: dict[str, JsonValue], index: int) -> dict[str, JsonValue]:
        node_id, steps = self._state(state)
        choices = self._nodes[node_id].choices
        if type(index) is not int or not 0 <= index < len(choices):
            raise QuestError("Такого варианта в этой сцене нет.")
        if steps >= MAX_STEPS:
            raise QuestError("Достигнут предел ходов квеста. Начните новую историю.")
        return {"node": self._resolve(choices[index].target), "steps": steps + 1}


class _Archive:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise QuestError("Квест слишком большой для загрузки.")
        self.members: dict[str, zipfile.ZipInfo] = {}
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                not entry.filename
                or len(entry.filename) > 256
                or entry.orig_filename != entry.filename
                or entry.filename.startswith("/")
                or "\\" in entry.filename
                or "\x00" in entry.filename
                or ".." in path.parts
                or ":" in entry.filename
                or path.as_posix() != entry.filename.rstrip("/")
                or entry.filename in self.members
                or stat.S_ISLNK(entry.external_attr >> 16)
                or entry.flag_bits & 1
                or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                or entry.file_size > MAX_MEMBER_BYTES
            ):
                raise QuestError("Недопустимый файл внутри квеста.")
            self.members[entry.filename] = entry

    def read(self, path: str, maximum: int) -> bytes:
        entry = self.members.get(path)
        if entry is None or entry.file_size > maximum:
            raise QuestError("Файл квеста отсутствует или слишком большой.")
        return self.archive.read(entry)

    def json(self, path: str) -> dict[str, object]:
        return _object(json.loads(self.read(path, MAX_JSON_BYTES), object_pairs_hook=_unique_object, parse_constant=_invalid_constant))

    def redirect(self, path: str) -> str:
        """Only an explicit, unconditional link is accepted from a script binding."""
        if not path.startswith("scripts/") or not path.endswith(".json"):
            raise _unsupported("неизвестный скрипт")
        data = self.json(path)
        _keys(data, {"id", "name", "blocks"})
        blocks = _list(data.get("blocks"), 1)
        if len(blocks) != 1:
            raise _unsupported("сложная логика переходов")
        event = _object(blocks[0])
        _keys(event, {"type", "id", "eventType", "children", "collapsed"})
        if event.get("type") != "event" or event.get("eventType") not in ("onNodeEnter", "onPress"):
            raise _unsupported("события сценария")
        children = _list(event.get("children"), 1)
        if len(children) != 1:
            raise _unsupported("сложная логика переходов")
        target = _object(children[0])
        _keys(target, {"type", "id", "node_id", "node_name"})
        if target.get("type") != "go_to_node":
            raise _unsupported("условия или изменение переменных")
        return _text(target.get("node_id"), 128)


def _background(value: object) -> str | None:
    if value in (None, ""):
        return None
    name = _text(value, 200)
    if "/" in name or "\\" in name or PurePosixPath(name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        raise _unsupported("формат фонового изображения")
    return f"res/images/{name}"


def _image(body: bytes) -> bytes:
    with Image.open(io.BytesIO(body)) as source:
        if source.format not in ("PNG", "JPEG", "WEBP") or source.width * source.height > MAX_IMAGE_PIXELS:
            raise _unsupported("формат или размер изображения")
        if getattr(source, "is_animated", False):
            raise _unsupported("анимированные иллюстрации")
        source.thumbnail((1280, 1280))
        rgba = ImageOps.exif_transpose(source).convert("RGBA")
        # Telegram photos reject extremely narrow aspect ratios.
        width = max(rgba.width, (rgba.height + 19) // 20)
        height = max(rgba.height, (rgba.width + 19) // 20)
        canvas = Image.new("RGB", (width, height), "white")
        canvas.paste(rgba, ((width - rgba.width) // 2, (height - rgba.height) // 2), mask=rgba.getchannel("A"))
        output = io.BytesIO()
        canvas.save(output, format="JPEG", quality=88)
        return output.getvalue()


def _node(data: dict[str, object], archive: _Archive, background: str | None, *, show_title: bool) -> _Node:
    _keys(data, _NODE_KEYS)
    title = _text(data.get("title", ""), 200, empty=True)
    content = _object(data.get("content"))
    _keys(content, {"items"})
    text: list[str] = [title] if title and show_title else []
    choices: list[_Choice] = []
    image_paths: list[str] = []
    redirect: str | None = None
    item_ids: set[str] = set()
    for raw in _list(content.get("items"), MAX_ITEMS):
        item = _object(raw)
        _keys(item, _ITEM_KEYS)
        item_id = _text(item.get("id"), 128)
        if item_id in item_ids:
            raise QuestError("В сцене повторяется идентификатор элемента.")
        item_ids.add(item_id)
        kind = item.get("type")
        if kind not in ("text", "image", "button", "script"):
            raise _unsupported("звук, ввод текста, таймеры или другие интерактивные элементы")
        triggers = _object(item.get("scriptTriggers") or {})
        if triggers and (kind != "button" or set(triggers) != {"onPress"}):
            raise _unsupported("события появления контента")
        if type(item.get("isHidden", False)) is not bool:
            raise QuestError("Некорректный признак видимости элемента.")
        target: str | None = None
        if triggers:
            target = archive.redirect(_text(triggers["onPress"], 256))
        if item.get("isHidden"):
            if kind == "script":
                raise _unsupported("скрытые скрипты")
            continue
        if kind == "text":
            value = _text(item.get("text", ""), MAX_SCENE_TEXT, empty=True)
            if value.strip():
                text.append(value)
        elif kind == "image":
            path = _text(item.get("resourcePath"), 256)
            if not path.startswith("res/images/"):
                raise _unsupported("внешние изображения")
            image_paths.append(path)
        elif kind == "button":
            label = _text(item.get("text"), 500)
            ordinary_target = item.get("targetNodeId")
            if target is not None and ordinary_target not in (None, "", target):
                raise _unsupported("кнопка с несколькими переходами")
            choices.append(_Choice(label, target or _text(ordinary_target, 128)))
        else:
            if redirect is not None:
                raise _unsupported("несколько автоматических переходов")
            redirect = archive.redirect(_text(item.get("resourcePath"), 256))
    if len(choices) > MAX_CHOICES or len(image_paths) > 1:
        raise _unsupported("слишком много кнопок или иллюстраций в одной сцене")
    if redirect is not None and (choices or len(text) > bool(title and show_title) or image_paths):
        raise _unsupported("автоматический переход вместе с содержимым сцены")
    result_text = "\n\n".join(text)
    if len(result_text) > MAX_SCENE_TEXT:
        raise QuestError("Текст сцены слишком большой.")
    if len(result_text.encode("utf-8")) + sum(len(choice.label.encode("utf-8")) for choice in choices) > MAX_SCENE_BYTES:
        raise QuestError("Текст и варианты сцены слишком большие для сохранения.")
    if redirect is None and not result_text.strip():
        raise _unsupported("сцена без текстового описания")
    return _Node(result_text, tuple(choices), image_paths[0] if image_paths else background, redirect)


def parse_mnd(body: bytes, story_id: str) -> MndBook:
    """Validate and freeze a bounded book; never extract files onto the filesystem."""
    if not body or len(body) > MAX_ARCHIVE_BYTES:
        raise QuestError("Квест слишком большой для загрузки.")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as source:
            archive = _Archive(source)
            config = archive.json("config.json")
            _keys(config, _CONFIG_KEYS)
            if config.get("id") != story_id:
                raise QuestError("Источник прислал другой квест.")
            if config.get("password") or config.get("pluginDependencies") or config.get("variables"):
                raise _unsupported("пароль, плагины или переменные")
            title = _text(config.get("title"), 200)
            author = _text(config.get("author"), 200)
            start = _text(config.get("startNodeId"), 128)
            hide_titles = config.get("hideNodeTitles", False)
            if type(hide_titles) is not bool:
                raise QuestError("Некорректный формат квеста.")
            chapters: dict[str, str | None] = {}
            for raw_chapter in _list(config.get("tags", []), MAX_NODES):
                chapter = _object(raw_chapter)
                _keys(chapter, {"id", "name", "cameraState", "backgroundAssetId", "backgroundAudioAssetId", "backgroundAudioVolume"})
                if chapter.get("backgroundAudioAssetId"):
                    raise _unsupported("фоновое аудио")
                chapter_id = _text(chapter.get("id"), 128)
                if chapter_id in chapters:
                    raise QuestError("В квесте повторяются главы.")
                chapters[chapter_id] = _background(chapter.get("backgroundAssetId"))
            embedded = _list(config.get("nodes", []), MAX_NODES)
            if "nodes.json" in archive.members:
                if embedded:
                    raise QuestError("В квесте два несовместимых списка сцен.")
                node_data = archive.json("nodes.json")
                _keys(node_data, {"nodes", "groups", "comments"})
                if node_data.get("groups") or node_data.get("comments"):
                    raise _unsupported("группы или комментарии редактора")
                raw_nodes = _list(node_data.get("nodes"), MAX_NODES)
            else:
                raw_nodes = embedded
            if not raw_nodes:
                raise QuestError("В квесте нет сцен.")
            default_background = _background(config.get("backgroundAssetId"))
            nodes: dict[str, _Node] = {}
            for raw_node in raw_nodes:
                data = _object(raw_node)
                node_id = _text(data.get("id"), 128)
                if node_id in nodes:
                    raise QuestError("В квесте повторяются сцены.")
                node_chapter = data.get("chapterId")
                if node_chapter is not None and not isinstance(node_chapter, str):
                    raise QuestError("Некорректная глава квеста.")
                background = _background(data.get("backgroundAssetId")) or chapters.get(node_chapter or "") or default_background
                nodes[node_id] = _node(data, archive, background, show_title=not hide_titles)
            images = {node.image_path for node in nodes.values() if node.image_path is not None and node.redirect is None}
            rendered: dict[str, bytes] = {}
            rendered_bytes = 0
            for path in sorted(images):
                image = _image(archive.read(path, MAX_MEMBER_BYTES))
                rendered_bytes += len(image)
                if rendered_bytes > MAX_ARCHIVE_BYTES:
                    raise QuestError("Иллюстрации квеста занимают слишком много памяти.")
                rendered[path] = image
            book = MndBook(story_id, title, author, hashlib.sha256(body).hexdigest(), start, nodes, rendered)
            book.start()
            # Validate unreachable branches too; do not wait for a chat to find a broken link.
            for node_id, node in nodes.items():
                book._resolve(node_id)
                for choice in node.choices:
                    book._resolve(choice.target)
            return book
    except QuestError:
        raise
    except (
        ValueError,
        TypeError,
        OSError,
        KeyError,
        RecursionError,
        zipfile.BadZipFile,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise QuestError("Файл квеста повреждён или имеет неподдерживаемый формат.") from exc
