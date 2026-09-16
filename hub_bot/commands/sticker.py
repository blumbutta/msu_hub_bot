import asyncio
import io
import json

import aiogram
import emoji
from PIL import Image, ImageOps
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import Message, ChatType
from aiogram.utils.markdown import hlink

from common.tg.filters import MetaInfo
from common.tg.utils import extract_image, download, download_by_file_id
from common.utils import image_bytes_io, FakeBytesIO
from utils.sticker_media import MAX_INPUT_BYTES, StickerMediaError, prepare_media

sticker_set_name_template = 'with_love_for_{id}_by_msu_hub_bot'
sticker_set_name_template_a = 'with_love_for_{id}a_by_msu_hub_bot'


class StickerStates(StatesGroup):
    sticker_set_name = State()


def only_emojis(text: str) -> str:
    d = {e['emoji']: True for e in emoji.emoji_list(text)}
    return ''.join(list(d)[:5])


class Stickers:
    @classmethod
    async def tgs(cls, message: Message):
        if not (target := message.reply_to_message):
            return None
        if not target.sticker:
            return None
        if not target.sticker.is_animated:
            return None
        return target.sticker.file_id, await target.sticker.download(destination_file=FakeBytesIO())

    @staticmethod
    def png_cut(f: io.BytesIO, squared: bool = False) -> io.BytesIO:
        image: Image.Image = Image.open(f)

        if squared:
            box = (512, 512)
        else:
            box = (512, image.height * 512 // image.width)
            if image.width < image.height:
                box = (image.width * 512 // image.height, 512)

        image = ImageOps.fit(image, box, Image.LANCZOS)
        return image_bytes_io(image, 'sticker', 'png')

    @classmethod
    async def png(cls, message: Message):
        target, dest = await extract_image(message, with_profile_photo=True)
        file = await download(dest)
        if file:
            return dest.file_id, cls.png_cut(file)

    @classmethod
    async def sticker_set_name(cls, message: Message, state: FSMContext):
        data = await state.get_data()
        if data.get('mixed_sticker'):
            return await cls.finish_chat_set(message, state, data)

        title = message.text or message.caption or ''
        if not title:
            return await message.reply(f'🎈 Выберите подходящее название для стикерпака или тыкните /cancel')

        async with state.proxy() as data:
            sticker_set_name = data['sticker_set_name']
            emojis = data['emojis']
            png = data['png']
            tgs = data['tgs']

        await state.finish()

        png = png and cls.png_cut(await download_by_file_id(png)) or None
        tgs = tgs and await download_by_file_id(tgs) or None

        try:
            await message.bot.create_new_sticker_set(user_id=message.from_user.id, name=sticker_set_name, emojis=emojis,
                                                     png_sticker=png, tgs_sticker=tgs, title=title)
            link = hlink('стикерпак', f'https://t.me/addstickers/{sticker_set_name}')
            await message.reply(f'✨ Ура, для вас был создан {link}. Управлять им можно через @Stickers.\n\n'
                                f'Имейте в виду, что на телефонах новые стикеры появляются с задержкой.')
            ss = await message.bot.get_sticker_set(sticker_set_name)
            return await message.reply_sticker(ss.stickers[-1].file_id)
        except aiogram.exceptions.InvalidPeerID:
            return await message.reply(f'🤷🏻‍♂️ Чтоб я смог создать стикерпак для вас, вам нужно начать личный чат со мной')
        except aiogram.exceptions.BadRequest:
            await message.reply(f'🤷🏻‍♂️ Произошла какая-то ошибка, подробнее в /error_stickers')
            raise

    @classmethod
    async def make_sticker(cls, message: Message, meta: MetaInfo, state: FSMContext, sticker_set_name: str, png=None, tgs=None):
        emojis = only_emojis(meta.extract_text()[1]) or '✨'

        try:
            try:
                await message.bot.get_sticker_set(sticker_set_name)
                await message.bot.add_sticker_to_set(user_id=message.from_user.id, name=sticker_set_name,
                                                     emojis=emojis, png_sticker=png and png[1], tgs_sticker=tgs and tgs[1])

            except aiogram.exceptions.InvalidStickersSet:
                await StickerStates.sticker_set_name.set()
                async with state.proxy() as data:
                    data['sticker_set_name'] = sticker_set_name
                    data['emojis'] = emojis
                    data['png'] = png and png[0] or ''
                    data['tgs'] = tgs and tgs[0] or ''
                return await message.reply(
                    f'🎈 Для вас еще не создан стикерпак. Придумайте ему название в следующем сообщении ⬇️, или тыкните /cancel. '
                    f'Учтите, что название стикерпака видят все и изменить его нельзя.')

        except aiogram.exceptions.BadRequest as e:
            if str(e) == 'Stickers_too_much':
                return await message.reply(f'🤷🏻‍♂️ Стикерпак заполнен. Удалить ненужный стикер можно командой /sd ответом на него.')
            else:
                await message.reply(f'🤷🏻‍♂️ Произошла какая-то ошибка, подробнее в /error_stickers')
                raise

        ss = await message.bot.get_sticker_set(sticker_set_name)
        return await message.reply_sticker(ss.stickers[-1].file_id)

    @staticmethod
    def source_media(message):
        # Prefer media attached to the command, then media in the replied message.
        for target in (message, message.reply_to_message):
            if not target:
                continue
            if target.sticker:
                sticker = target.sticker
                if getattr(sticker, 'type', 'regular') != 'regular':
                    raise StickerMediaError('Пришлите обычный стикер, а не маску или custom emoji.')
                kind = 'animated' if sticker.is_animated else 'video' if sticker.is_video else 'static'
                return sticker, kind
            if target.animation or target.video or target.video_note:
                return target.animation or target.video or target.video_note, 'video'
            if target.photo:
                return target.photo[-1], 'static'
            if target.document:
                doc = target.document
                mime = (doc.mime_type or '').lower()
                if mime.startswith('video/') or mime == 'image/gif':
                    return doc, 'video'
                if mime.startswith('image/'):
                    return doc, 'static'
        return None, None

    @classmethod
    async def make_chat_sticker(cls, message, meta, state, name):
        try:
            source, kind = cls.source_media(message)
            if source is None:
                # Preserve the old avatar fallback only when no media was supplied.
                _, source = await extract_image(message, with_profile_photo=True)
                kind = 'static'
            if source is None:
                return await message.reply('Ответьте /sc на картинку, GIF, видео или стикер.')
            if (source.file_size or 0) > MAX_INPUT_BYTES:
                raise StickerMediaError('Файл больше 20 МБ. Стикер не добавлен.')
            file = await download(source)
            if file is None:
                raise StickerMediaError('Не удалось скачать файл. Стикер не добавлен.')
            # FFmpeg runs off the event loop. Its individual subprocesses have timeouts.
            kind, payload = await asyncio.to_thread(prepare_media, file.getvalue(), kind)
            emojis = list(dict.fromkeys(e['emoji'] for e in emoji.emoji_list(meta.extract_text()[1])))[:5] or ['✨']
            suffix = {'static': 'webp', 'animated': 'tgs', 'video': 'webm'}[kind]
            # Modern InputSticker supports mixed packs without migrating aiogram 2.
            uploaded = await message.bot.request('uploadStickerFile', {
                'user_id': message.from_user.id, 'sticker_format': kind,
            }, files={'sticker': (f'sticker.{suffix}', io.BytesIO(payload))})
            sticker = {'sticker': uploaded['file_id'], 'format': kind, 'emoji_list': emojis}
            try:
                await message.bot.get_sticker_set(name)
            except aiogram.exceptions.InvalidStickersSet:
                await state.update_data(mixed_sticker=sticker, sticker_set_name=name,
                                        sticker_chat_id=message.chat.id, sticker_user_id=message.from_user.id)
                await state.set_state(StickerStates.sticker_set_name.state)
                return await message.reply('🎈 Пришлите название стикерпака (1–64 символа) или /cancel.')
            await message.bot.request('addStickerToSet', {
                'user_id': message.from_user.id, 'name': name,
                'sticker': json.dumps(sticker, ensure_ascii=False),
            })
            return await message.reply_sticker(uploaded['file_id'])
        except StickerMediaError as exc:
            return await message.reply(str(exc))
        except aiogram.exceptions.InvalidPeerID:
            return await message.reply('Сначала начните личный чат со мной, затем повторите /sc.')
        except aiogram.exceptions.BadRequest:
            await message.reply('Не удалось добавить стикер. Подробнее в /error_stickers.')
            raise

    @classmethod
    async def finish_chat_set(cls, message, state, data):
        if (message.chat.id != data['sticker_chat_id']
                or message.from_user.id != data['sticker_user_id']):
            return await message.reply('Название должен прислать автор команды в том же чате.')
        if not await can_edit_chat_stickers(message):
            return await message.reply('Стикерпак чата могут редактировать только его админы.')
        title = (message.text or message.caption or '').strip()
        if not 1 <= len(title) <= 64:
            return await message.reply('Название должно содержать от 1 до 64 символов. Или /cancel.')
        name = data['sticker_set_name']
        try:
            # Another admin may have created the shared pack while we awaited a title.
            try:
                await message.bot.get_sticker_set(name)
            except aiogram.exceptions.InvalidStickersSet:
                await message.bot.request('createNewStickerSet', {
                    'user_id': message.from_user.id, 'name': name, 'title': title,
                    'stickers': json.dumps([data['mixed_sticker']], ensure_ascii=False),
                    'sticker_type': 'regular',
                })
            else:
                await message.bot.request('addStickerToSet', {
                    'user_id': message.from_user.id, 'name': name,
                    'sticker': json.dumps(data['mixed_sticker'], ensure_ascii=False),
                })
        except aiogram.exceptions.InvalidPeerID:
            return await message.reply('Начните личный чат со мной и снова пришлите название.')
        except aiogram.exceptions.BadRequest:
            await message.reply('Не удалось сохранить стикер. Попробуйте ещё раз или /cancel. Подробнее в /error_stickers.')
            raise
        await state.finish()
        await message.reply(hlink('✨ Стикерпак чата', f'https://t.me/addstickers/{name}'))
        return await message.reply_sticker(data['mixed_sticker']['sticker'])

    @classmethod
    async def make_sticker_png(cls, message: Message, meta: MetaInfo, state: FSMContext, sticker_set_name: str):
        sticker = await cls.png(message)
        if sticker:
            return await cls.make_sticker(message, meta, state, sticker_set_name, png=sticker)

    @classmethod
    async def make_sticker_tgs(cls, message: Message, meta: MetaInfo, state: FSMContext, sticker_set_name: str):
        sticker = await cls.tgs(message)
        if sticker:
            return await cls.make_sticker(message, meta, state, sticker_set_name, tgs=sticker)


async def process_sticker(message: Message, meta: MetaInfo, state: FSMContext):
    sticker_set_name = sticker_set_name_template.format(id=message.from_user.id)
    return await Stickers.make_sticker_png(message, meta, state, sticker_set_name)


async def can_edit_chat_stickers(message):
    if message.chat.type == ChatType.PRIVATE or not message.from_user or message.sender_chat:
        return False
    if message.chat.all_members_are_administrators:
        return True
    admins = await message.chat.get_administrators()
    return message.from_user.id in (a.user.id for a in admins)


async def process_sticker_chat(message: Message, meta: MetaInfo, state: FSMContext):
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply('🤷🏻‍♂️ Эта команда только для чатов')
    if not await can_edit_chat_stickers(message):
        return await message.reply('🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы')
    name = sticker_set_name_template.format(id=abs(message.chat.id))
    return await Stickers.make_chat_sticker(message, meta, state, name)


async def process_animated_sticker(message: Message, meta: MetaInfo, state: FSMContext):
    sticker_set_name = sticker_set_name_template_a.format(id=message.from_user.id)
    return await Stickers.make_sticker_tgs(message, meta, state, sticker_set_name)


async def process_animated_sticker_chat(message: Message, meta: MetaInfo, state: FSMContext):
    if message.chat.type == ChatType.PRIVATE:
        return await message.reply(f'🤷🏻‍♂️ Эта команда только для чатов')

    if not message.chat.all_members_are_administrators:
        admins = await message.chat.get_administrators()
        if message.from_user.id not in (a.user.id for a in admins):
            return await message.reply(f'🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы')

    sticker_set_name = sticker_set_name_template_a.format(id=abs(message.chat.id))
    return await Stickers.make_sticker_tgs(message, meta, state, sticker_set_name)


async def process_sticker_delete(message: Message):
    if not (target := message.reply_to_message):
        return True
    if not (sticker := target.sticker):
        return True

    name = sticker.set_name or ''
    link = hlink('пака', f'https://t.me/addstickers/{name}')

    if not name.endswith('_by_msu_hub_bot'):
        return await message.reply('🤷🏻‍♂️ Этот стикерпак создан не мной, попробуйте через @Stickers')

    if name in (sticker_set_name_template.format(id=message.from_user.id),
                sticker_set_name_template_a.format(id=message.from_user.id)):
        await sticker.delete_from_set()
        return await message.reply(f'✅ Стикер удален из {link}, в течение часа он пропадет из набора у всех пользователей')

    if name in (sticker_set_name_template.format(id=abs(message.chat.id)),
                sticker_set_name_template_a.format(id=abs(message.chat.id))):
        if not await can_edit_chat_stickers(message):
            return await message.reply(f'🤷🏻‍♂️ Стикерпак чата могут редактировать только его админы')
        # Telegram deletes static, TGS and WEBM stickers by the same file_id.
        await sticker.delete_from_set()
        return await message.reply(f'✅ Стикер удален из {link}, в течение часа он пропадет из набора у всех пользователей')

    return await message.reply('🤷🏻‍♂️ Судя по всему, стикерпак создан другим пользователем или в другом чате, '
                               'где создавали — там и удаляйте')
