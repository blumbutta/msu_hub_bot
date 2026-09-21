"""A short original story for exercising the same runtime as imported quests."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from msu_hub_bot.media.quest_scene import DemoPicture, render_demo_scene
from msu_hub_bot.providers.quest import QuestError, QuestScene


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    node: Literal["shore", "yard", "tower", "boat", "rescued", "shelter"] = "shore"
    item: Literal["none", "key", "fuel", "radio"] = "none"


class DemoQuestBook:
    id = "demo"
    title = "Огни на мысе · демо"
    author = "Оригинальный демонстрационный сценарий"
    digest = hashlib.sha256(b"msu-hub-quest-demo-v1").hexdigest()

    @staticmethod
    def start() -> dict[str, JsonValue]:
        return _State().model_dump(mode="json")

    @staticmethod
    def _state(value: dict[str, JsonValue]) -> _State:
        try:
            state = _State.model_validate(value)
            if (state.node == "shore") != (state.item == "none"):
                raise ValueError("Invalid demo progress")
            return state
        except ValidationError, ValueError:
            raise QuestError("Не удалось прочитать сохранение демо-квеста.") from None

    def view(self, state: dict[str, JsonValue]) -> QuestScene:
        progress = self._state(state)
        choices: tuple[str, ...]
        picture: DemoPicture = progress.node
        if progress.node == "shore":
            text = (
                "Шторм отрезал вас от берега. Над мысом стоит погасший маяк. Вдалеке слышен мотор спасательного катера, "
                "но в темноте вас не заметят.\n\nУ двери висит связка ключей, у генератора лежит канистра, "
                "а в будке смотрителя мерцает рация. Что проверим первым?"
            )
            choices = ("🔑 Взять ключи", "⛽ Забрать канистру", "📻 Проверить рацию")
        elif progress.node == "yard":
            finding = {
                "key": "У вас ключ от верхней площадки. На бирке написано: «Ручная сигнальная лампа — в шкафу». ",
                "fuel": "В канистре осталось топливо. Его хватит на один запуск генератора. ",
                "radio": "Рация работает! Спасатели просят дать сигнал с высоты или с открытой воды. ",
                "none": "",
            }[progress.item]
            text = finding + "\n\nВолны подбираются к двору. Можно подняться к фонарю маяка или выйти к старой лодке. Куда идём?"
            choices = ("🗼 Подняться на маяк", "🚣 Проверить лодку")
        elif progress.node == "tower":
            action = {
                "key": "🔦 Открыть шкаф и подать сигнал лампой",
                "fuel": "⚙️ Запустить генератор маяка",
                "radio": "📻 Передать координаты с площадки",
                "none": "",
            }[progress.item]
            text = "С площадки виден спасательный катер. Он проходит мимо мыса. У вас есть шанс привлечь его внимание."
            choices = (action, "🏠 Переждать шторм в доме смотрителя")
        elif progress.node == "boat":
            text = "Лодка цела, но волны слишком высокие. Можно попытаться уйти с мыса сейчас или вернуться в дом смотрителя."
            choices = ("🚣 Выйти в море", "🏠 Остаться в укрытии")
        elif progress.node == "rescued":
            reason = {
                "key": "Вы открыли шкаф и дали сигнал ручной лампой.",
                "fuel": "Топливо позволило зажечь маяк.",
                "radio": "С верхней площадки удалось передать координаты без помех.",
                "none": "",
            }[progress.item]
            text = f"🏁 Все спаслись!\n\n{reason} Катер заметил вас и подошёл к защищённой стороне мыса. Ваш первый выбор помог выбраться."
            choices = ()
        else:
            text = (
                "🏁 До рассвета.\n\nВы остались на мысе. В доме смотрителя нашлись одеяла и запас воды. "
                "Утром шторм стих, и рыбаки забрали вас. Все целы, но ночь оказалась долгой."
            )
            choices = ()
        return QuestScene(text=text, choices=choices, image=render_demo_scene(picture))

    def choose(self, state: dict[str, JsonValue], index: int) -> dict[str, JsonValue]:
        progress = self._state(state)
        if type(index) is not int or not 0 <= index < len(self.view(state).choices):
            raise QuestError("Этот вариант больше недоступен.")
        if progress.node == "shore":
            return _State(node="yard", item=("key", "fuel", "radio")[index]).model_dump(mode="json")
        if progress.node == "yard":
            return _State(node="tower" if index == 0 else "boat", item=progress.item).model_dump(mode="json")
        # An unsafe boat trip returns the group to shelter; it never skips a vote.
        return _State(node="rescued" if progress.node == "tower" and index == 0 else "shelter", item=progress.item).model_dump(mode="json")


def demo_book() -> DemoQuestBook:
    return DemoQuestBook()
