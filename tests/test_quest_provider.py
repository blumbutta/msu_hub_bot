import asyncio
import copy
import hashlib
import io
import json
import stat
import zipfile

import pytest
from PIL import Image

from msu_hub_bot.providers import quest as source
from msu_hub_bot.providers import quest_runtime as runtime
from msu_hub_bot.providers.quest import QuestError, QuestProvider, TRUST_STORY_ID


def story():
    config = {
        "id": TRUST_STORY_ID,
        "title": "Тестовая история",
        "author": "Автор теста",
        "startNodeId": "start",
        "tags": [{"id": "chapter", "backgroundAssetId": "background.png"}],
        "nodes": [],
    }
    nodes = [
        {
            "id": "start",
            "chapterId": "chapter",
            "title": "Развилка",
            "content": {
                "items": [
                    {"id": "text", "type": "text", "text": "Куда пойдём?"},
                    {"id": "left", "type": "button", "text": "Налево", "targetNodeId": "finish"},
                    {"id": "right", "type": "button", "text": "Направо", "targetNodeId": "another"},
                ]
            },
        },
        {"id": "finish", "title": "Финал", "content": {"items": [{"id": "end", "type": "text", "text": "Вы спаслись."}]}},
        {"id": "another", "title": "Другой финал", "content": {"items": [{"id": "end", "type": "text", "text": "Нашли сокровище."}]}},
    ]
    return config, nodes


def png(color="navy"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


def archive(config=None, nodes=None, *, extra=None):
    default_config, default_nodes = story()
    config = default_config if config is None else config
    nodes = default_nodes if nodes is None else nodes
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
        target.writestr("config.json", json.dumps(config, ensure_ascii=False))
        target.writestr("nodes.json", json.dumps({"nodes": nodes}, ensure_ascii=False))
        target.writestr("res/images/background.png", png())
        for path, body in (extra or {}).items():
            target.writestr(path, body)
    return output.getvalue()


def test_book_uses_real_choices_distinct_endings_image_fallback_and_immutable_state():
    body = archive()
    book = runtime.parse_mnd(body, TRUST_STORY_ID)
    state = book.start()
    initial = copy.deepcopy(state)
    scene = book.view(state)
    assert scene.text == "Развилка\n\nКуда пойдём?"
    assert scene.choices == ("Налево", "Направо")
    with Image.open(io.BytesIO(scene.image)) as image:
        assert image.format == "JPEG" and image.size == (32, 24)
    left, right = book.choose(state, 0), book.choose(state, 1)
    assert state == initial
    assert book.view(left).choices == book.view(right).choices == ()
    assert book.view(left).text != book.view(right).text
    assert book.digest == hashlib.sha256(body).hexdigest()
    restored = runtime.parse_mnd(body, TRUST_STORY_ID)
    assert restored.view(left) == book.view(left)


def test_inline_image_overrides_background_and_reencodes_bytes():
    config, nodes = story()
    nodes[0]["content"]["items"].insert(0, {"id": "image", "type": "image", "resourcePath": "res/images/scene.png"})
    book = runtime.parse_mnd(archive(config, nodes, extra={"res/images/scene.png": png("red")}), TRUST_STORY_ID)
    with Image.open(io.BytesIO(book.view(book.start()).image)) as image:
        assert image.getpixel((0, 0))[0] > 200


def test_image_is_padded_for_telegram_photo_limits():
    output = io.BytesIO()
    Image.new("RGB", (1, 400), "navy").save(output, format="PNG")
    with Image.open(io.BytesIO(runtime._image(output.getvalue()))) as image:
        assert image.width * 20 >= image.height


def test_animated_or_invalid_image_cannot_reach_telegram():
    output = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(
        output, format="PNG", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")], duration=100
    )
    for body in (output.getvalue(), b"not an image"):
        config, nodes = story()
        nodes[0]["content"]["items"].append({"id": "photo", "type": "image", "resourcePath": "res/images/extra.png"})
        with pytest.raises(QuestError):
            runtime.parse_mnd(archive(config, nodes, extra={"res/images/extra.png": body}), TRUST_STORY_ID)


def link_script(target="finish", *, event="onNodeEnter"):
    return json.dumps({"blocks": [{"type": "event", "eventType": event, "children": [{"type": "go_to_node", "node_id": target}]}]})


def test_bound_button_unconditional_script_is_a_link_not_executable_code():
    config, nodes = story()
    nodes[0]["content"]["items"][1] = {"id": "left", "type": "button", "text": "Налево", "scriptTriggers": {"onPress": "scripts/link.json"}}
    book = runtime.parse_mnd(archive(config, nodes, extra={"scripts/link.json": link_script()}), TRUST_STORY_ID)
    assert book.choose(book.start(), 0)["node"] == "finish"


def test_unconditional_entry_script_resolves_without_an_empty_scene():
    config, nodes = story()
    nodes[0]["content"]["items"] = [{"id": "script", "type": "script", "resourcePath": "scripts/link.json"}]
    book = runtime.parse_mnd(archive(config, nodes, extra={"scripts/link.json": link_script()}), TRUST_STORY_ID)
    assert book.start() == {"node": "finish", "steps": 0}


@pytest.mark.parametrize("feature", ["input", "timer", "audio", "lua", "javascript"])
def test_unsupported_element_rejected_even_in_unreachable_branch(feature):
    config, nodes = story()
    nodes.append({"id": "hidden-branch", "content": {"items": [{"id": "feature", "type": feature, "isHidden": True}]}})
    with pytest.raises(QuestError, match="не поддерживается"):
        runtime.parse_mnd(archive(config, nodes), TRUST_STORY_ID)


@pytest.mark.parametrize(
    "field,value", [("variables", [{"name": "score", "defaultValue": 0}]), ("pluginDependencies", ["plugin"]), ("password", "protected")]
)
def test_control_features_are_rejected_up_front(field, value):
    config, nodes = story()
    config[field] = value
    with pytest.raises(QuestError, match="не поддерживается"):
        runtime.parse_mnd(archive(config, nodes), TRUST_STORY_ID)


@pytest.mark.parametrize("kind", ["condition", "assign_variable", "http_request", "execute"])
def test_script_can_never_evaluate_expressions_or_arbitrary_operations(kind):
    config, nodes = story()
    nodes[0]["content"]["items"] = [{"id": "script", "type": "script", "resourcePath": "scripts/link.json"}]
    script = {"blocks": [{"type": "event", "eventType": "onNodeEnter", "children": [{"type": kind, "expression": "__import__('os')"}]}]}
    with pytest.raises(QuestError, match="не поддерживается"):
        runtime.parse_mnd(archive(config, nodes, extra={"scripts/link.json": json.dumps(script)}), TRUST_STORY_ID)


@pytest.mark.parametrize("target", ["missing", "start"])
def test_broken_links_and_automatic_cycles_are_rejected_before_start(target):
    config, nodes = story()
    nodes[0]["content"]["items"] = [{"id": "script", "type": "script", "resourcePath": "scripts/link.json"}]
    with pytest.raises(QuestError):
        runtime.parse_mnd(archive(config, nodes, extra={"scripts/link.json": link_script(target)}), TRUST_STORY_ID)


@pytest.mark.parametrize("path", ["../outside", "/outside", "res/../../outside", "res\\outside", "C:/outside"])
def test_archive_paths_cannot_escape_or_use_absolute_targets(path):
    with pytest.raises(QuestError, match="Недопустимый файл"):
        runtime.parse_mnd(archive(extra={path: "anything"}), TRUST_STORY_ID)


def test_zip_symlink_and_duplicate_members_are_rejected():
    for variant in ("link", "duplicate"):
        output = io.BytesIO(archive())
        with zipfile.ZipFile(output, "a") as target:
            if variant == "link":
                entry = zipfile.ZipInfo("res/symlink")
                entry.create_system = 3
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
                target.writestr(entry, "/etc/passwd")
            else:
                with pytest.warns(UserWarning, match="Duplicate name"):
                    target.writestr("config.json", "{}")
        with pytest.raises(QuestError, match="Недопустимый файл"):
            runtime.parse_mnd(output.getvalue(), TRUST_STORY_ID)


def test_archive_json_image_and_expansion_limits(monkeypatch):
    body = archive()
    for name, value in [
        ("MAX_ARCHIVE_BYTES", 1),
        ("MAX_EXPANDED_BYTES", 1),
        ("MAX_MEMBER_BYTES", 1),
        ("MAX_JSON_BYTES", 1),
        ("MAX_ENTRIES", 1),
        ("MAX_IMAGE_PIXELS", 1),
    ]:
        with monkeypatch.context() as patch:
            patch.setattr(runtime, name, value)
            with pytest.raises(QuestError):
                runtime.parse_mnd(body, TRUST_STORY_ID)


def test_scene_storage_budget_counts_utf8_text_and_all_choice_labels():
    config, nodes = story()
    nodes[0]["content"]["items"][0]["text"] = "🦊" * (runtime.MAX_SCENE_BYTES // 4)
    assert len(nodes[0]["content"]["items"][0]["text"]) < runtime.MAX_SCENE_TEXT
    with pytest.raises(QuestError, match="для сохранения"):
        runtime.parse_mnd(archive(config, nodes), TRUST_STORY_ID)


def test_duplicate_nodes_missing_images_external_images_and_wrong_book_rejected():
    config, nodes = story()
    for mutation in ("duplicate", "missing-image", "external-image", "wrong-id"):
        conf, ns = copy.deepcopy(config), copy.deepcopy(nodes)
        if mutation == "duplicate":
            ns.append(copy.deepcopy(ns[0]))
        elif mutation.endswith("image"):
            path = "https://localhost/private" if mutation == "external-image" else "res/images/missing.png"
            ns[0]["content"]["items"].append({"id": "photo", "type": "image", "resourcePath": path})
        else:
            conf["id"] = "other-book"
        with pytest.raises(QuestError):
            runtime.parse_mnd(archive(conf, ns), TRUST_STORY_ID)


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"node": "missing", "steps": 1},
        {"node": "start", "steps": True},
        {"node": "start", "steps": -1},
        {"node": "start", "steps": 0, "extra": 1},
    ],
)
def test_saved_state_is_bounded_and_validated(state):
    book = runtime.parse_mnd(archive(), TRUST_STORY_ID)
    with pytest.raises(QuestError, match="Сохранение"):
        book.view(state)


@pytest.mark.parametrize("index", [-1, 2, True, "0"])
def test_choice_index_is_not_coerced(index):
    book = runtime.parse_mnd(archive(), TRUST_STORY_ID)
    with pytest.raises(QuestError, match="варианта"):
        book.choose(book.start(), index)


class Response:
    def __init__(self, body, status=200, declared=None):
        self.body = body
        self.status = status
        self.content_length = len(body) if declared is None else declared
        self.content = self
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


async def test_provider_uses_only_official_url_does_not_follow_redirects_and_closes(monkeypatch):
    body = archive()
    response = Response(body)
    session = Session([response])
    monkeypatch.setattr(source.aiohttp, "ClientSession", lambda **kwargs: session)
    provider = QuestProvider()
    book = await provider.load(TRUST_STORY_ID)
    assert await provider.load(TRUST_STORY_ID, book.digest) is book
    assert session.calls == [(f"{source.API_URL}/{TRUST_STORY_ID}/file", {"allow_redirects": False})]
    assert response.closed
    with pytest.raises(QuestError, match="обновил"):
        await provider.load(TRUST_STORY_ID, "0" * 64)
    await provider.close()
    assert session.closed and not provider._cache
    with pytest.raises(QuestError, match="закрыт"):
        await provider.load(TRUST_STORY_ID)


async def test_digest_survives_provider_restart_and_rejects_updated_book(monkeypatch):
    body = archive()
    original = runtime.parse_mnd(body, TRUST_STORY_ID)
    config, nodes = story()
    nodes[1]["content"]["items"][0]["text"] = "Автор переписал финал."
    sessions = [Session([Response(body)]), Session([Response(archive(config, nodes))])]
    monkeypatch.setattr(source.aiohttp, "ClientSession", lambda **kwargs: sessions.pop(0))
    provider = QuestProvider()
    assert (await provider.load(TRUST_STORY_ID, original.digest)).digest == original.digest
    await provider.close()
    provider = QuestProvider()
    with pytest.raises(QuestError, match="обновил"):
        await provider.load(TRUST_STORY_ID, original.digest)
    assert not provider._cache
    await provider.close()


async def test_cache_keeps_at_most_two_books(monkeypatch):
    provider = QuestProvider()
    identifiers = ["00000000-0000-0000-0000-00000000000" + str(i) for i in range(3)]

    async def download(ident):
        config, nodes = story()
        config["id"] = ident
        return archive(config, nodes)

    monkeypatch.setattr(provider, "_download", download)
    for ident in identifiers:
        await provider.load(ident)
    assert list(provider._cache) == identifiers[-2:]
    await provider.close()


@pytest.mark.parametrize("declared", [None, 0])
async def test_download_size_is_checked_with_and_without_content_length(monkeypatch, declared):
    monkeypatch.setattr(runtime, "MAX_ARCHIVE_BYTES", 10)
    response = Response(b"a" * 11, declared=declared)
    session = Session([response])
    monkeypatch.setattr(source.aiohttp, "ClientSession", lambda **kwargs: session)
    provider = QuestProvider()
    with pytest.raises(QuestError):
        await provider.load(TRUST_STORY_ID)
    assert response.closed
    await provider.close()


@pytest.mark.parametrize("ident", ["https://evil.invalid/story", "../etc/passwd", "DEADBEEF-DEAD-BEEF-DEAD-BEEFDEADBEEF", "not-a-uuid"])
async def test_provider_rejects_user_urls_before_network(ident):
    provider = QuestProvider()
    with pytest.raises(QuestError, match="Укажите ID"):
        await provider.load(ident)
    assert provider._session is None
    await provider.close()


@pytest.mark.parametrize("status", [301, 302, 403, 429, 500])
async def test_provider_rejects_error_and_redirect_responses(monkeypatch, status):
    response = Response(b"", status=status)
    session = Session([response])
    monkeypatch.setattr(source.aiohttp, "ClientSession", lambda **kwargs: session)
    provider = QuestProvider()
    with pytest.raises(QuestError, match="Не удалось загрузить"):
        await provider.load(TRUST_STORY_ID)
    assert response.closed
    await provider.close()


async def test_download_and_queue_are_inside_deadline(monkeypatch):
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.02)
    provider = QuestProvider()
    started = asyncio.Event()

    async def slow(_):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_download", slow)
    first = asyncio.create_task(provider.load(TRUST_STORY_ID))
    await started.wait()
    second = asyncio.create_task(provider.load(TRUST_STORY_ID))
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, QuestError) for result in results)
    assert not provider._lock.locked()
    await provider.close()


async def test_timed_out_parse_cannot_stack_background_workers(monkeypatch):
    monkeypatch.setattr(source, "FETCH_TIMEOUT", 0.02)
    provider = QuestProvider()
    release = asyncio.Event()
    calls = 0

    async def download(_):
        return b"ignored"

    async def parse(body, ident):
        nonlocal calls
        calls += 1
        await release.wait()
        return runtime.parse_mnd(archive(), TRUST_STORY_ID)

    monkeypatch.setattr(provider, "_download", download)
    monkeypatch.setattr(provider, "_parse", parse)
    for _ in range(2):
        with pytest.raises(QuestError, match="Не удалось загрузить"):
            await provider.load(TRUST_STORY_ID)
    assert calls == 1
    release.set()
    await provider.close()


async def test_demo_is_offline_and_pin_is_checked():
    provider = QuestProvider()
    book = await provider.load("demo")
    assert book.id == "demo" and book.view(book.start()).choices
    assert provider._session is None
    with pytest.raises(QuestError, match="обновил"):
        await provider.load("demo", "0" * 64)
    await provider.close()
