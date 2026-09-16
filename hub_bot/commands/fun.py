import random
import re
import time
from collections import defaultdict
from itertools import permutations
from typing import Final, Tuple

import pendulum
from aiogram.dispatcher.handler import SkipHandler
from aiogram.types import Message
from aiogram.utils.markdown import hpre, hbold

from common.tg.filters import MetaInfo
from common.utils import percent_chance

lyrics = '''(Припев):
Водка, пиво, водка, пиво — под конец корпоратива
Под восточные мотивы выполняем нормативы!
Водка, пиво, водка, пиво — под конец корпоратива
Ты уходишь, так красиво, было круто, всем спасибо!

Джин и тоник, джин и тоник, я в душе такой разбойник!
Не поверишь, сам не верю, но джин тоник в это верит
Спрайт, текила, спрайт, текила — меня в овощ превратила
Аккуратно уложила, нежно лавашом накрыла.

Водка с соком помидорным, ну не будь таким упорным!
Просто покраснели глазки, от двух капелек табаски

(Припев)

После соточки абсента, стали все говорить с акцентам
И зачем образованье, для взаимопониманья?
Шоколадный, есть вишневый и, конечно же, миндальный!
Пробуй чачу, угощаю, ничего ни запрещаю
Хватит пить, поешь закуски, я уж понял, что ты русский,
И поэтому, брателла, от Вахбета — Изабелла!

(Припев)
'''


async def process_beer(message: Message):
    return await message.reply_audio(audio='https://t.me/mechmath/625715', caption=hpre(lyrics))


class PokakatsData:
    tz = pendulum.timezone('Europe/Moscow')

    def __init__(self):
        self.until_ts = pendulum.tomorrow(tz=self.tz).timestamp()
        self.users = defaultdict(int)
        self.chats = defaultdict(int)

    def plus(self, user_id: int, chat_id: int, count: int = 1) -> Tuple[int, int]:
        curr_ts = time.time()
        if curr_ts > self.until_ts:
            self.__init__()
        self.users[user_id] += count
        self.chats[chat_id] += count
        return self.users[user_id], self.chats[chat_id]


pokakats = PokakatsData()


async def process_pokakats(message: Message):
    emojis: Final = ('💩', '🧻', '🚽', '🚻', '🚾')

    def postfix(count: int) -> str:
        count = count % 100
        if count in range(5, 21):
            return 'каканий'
        count = count % 10
        if count == 1:
            return 'какание'
        if count in (2, 3, 4):
            return 'какания'
        return 'каканий'

    if message.sticker:
        if message.sticker.emoji not in emojis and message.sticker.file_unique_id not in (
                # https://t.me/addstickers/peepo_pack
                'AgADIwADtEzqKA',
                'AgADUgADtEzqKA',
                'AgADUwADtEzqKA',
                'AgADVAADtEzqKA',

                # https://t.me/addstickers/nanopeepo
                'AgADWAIAAh_2ths',
                'AgADbgIAAh_2ths',
                'AgADbwIAAh_2ths',
                'AgADcAIAAh_2ths',
                'AgADvwIAAh_2ths',
        ):
            raise SkipHandler()
    elif text := message.text:
        if text not in emojis:
            raise SkipHandler()
    else:
        raise SkipHandler()

    count, note = 1, ''
    if percent_chance(5.):
        count, note = 3, '+3, очень хорошо покакали!'

    user_count, chat_count = pokakats.plus(message.from_user.id, message.chat.id, count)
    text = f'{random.choice(emojis)} Сегодня у тебя {hbold(user_count)} {postfix(user_count)} и {hbold(chat_count)} {postfix(chat_count)} у чата\n\n{note}'
    return await message.reply(text)


async def process_puk(_message: Message, meta: MetaInfo):
    target, text = meta.extract_text()
    if not text:
        return True

    puk_variants = '|'.join(''.join(p) for p in permutations('куп'))
    perd_variants = '|'.join(''.join(p) for p in permutations('епрд'))
    kal_variants = '|'.join(''.join(p) for p in permutations('кал'))

    text, n = re.subn(rf'{puk_variants}|(пу\w?)|(п\w?к)|(\w?ук)', 'пук', text)
    text, m = re.subn(rf'{perd_variants}|(пер\w?)|(пе\w?д)|(п\w?рд)|(\w?ерд)', 'перд', text)
    text, k = re.subn(rf'{kal_variants}|(ка\w?)|(к\w?л)|(\w?ал)', 'кал', text)
    if n or m or k:
        return await target.reply(text)
    return True
