from msu_hub_bot.settings import settings, MissingIntegration
from msu_hub_bot.redaction import redact

import os
import random
import traceback

import aiogram
from aiogram.dispatcher import Dispatcher, FSMContext
from aiogram.dispatcher.filters import IDFilter, Text
from aiogram.dispatcher.filters.filters import NotFilter
from aiogram.types import ContentType, Message, Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils import executor
from aiogram.utils.markdown import hbold, hpre
from cachetools import TTLCache

from app import app, bot, dp, logger, redis, wit, wolfram
from commands.admin import process_ban, process_forward_builder, process_forwards, process_restrict, process_revoke, \
    process_sudo, process_unban
from commands.animate import process_animate, process_matrix
from commands.antibot import AntiBot
from commands.arxiv import process_arxiv
from commands.balaboba import Balaboba
from commands.camera import Camera
from commands.crypto import Crypto
from commands.dalle import process_dalle_emoji, process_rudalle
from commands.debate import Debate
from commands.debug import process_delete_after, process_json, process_logs
from commands.deepdream import process_deepdream
from commands.dvach import Dvach
from commands.errors import process_error_stickers, process_donate, process_supporters
from commands.excuses import process_excuse
from commands.externals import process_badwiki, process_toonify, process_bg, process_zombie, process_srgan, \
    process_waifu, process_porfirevich, \
    process_cheatsheet, process_topdf, process_orfogrammka_quality, process_orfogrammka_cicero, \
    process_orfogrammka_common, process_imgur, process_duckduckgo, process_gpt3, process_nudes, process_demographic, \
    process_colorizer, process_agile_gan, \
    process_which_anime, process_bored, process_anime, process_drag, process_ud, process_arcane, process_what_hg, \
    process_doll, process_fake_voice, process_vintage, process_privacy, process_paint, process_inter, process_copilot, \
    process_gptn, process_dalle, \
    process_rugpt3, process_openai_gpt3, process_latent_diffusion, process_inpaint, process_stable_diffusion
from commands.figlet import process_figlet
from commands.fun import process_beer, process_pokakats, process_puk
from commands.genders import process_gender
from commands.geoguess import Geoguess
from commands.help import HelpMessage
from commands.infra import process_create_infra_chat, process_delete_infra_chat, process_inline, process_links, \
    process_pin, \
    process_pin_all, process_status, process_update_pins
from commands.latex import Latex
from commands.likes import Like
from commands.lingvanex import process_langs, process_translate, process_en, process_ru
from commands.lobster import process_lobster, process_demotivator, process_atmta, process_atmta_v
from commands.location import process_location
from commands.minecraft import MinecraftStatus
from commands.other import process_me, process_transliterate, process_punto, process_md, process_html, process_id, \
    process_copy, process_file_id
from commands.personal import process_sanya, process_dyubs, process_pookie_pook, process_popov
from commands.posting import process_post_all, process_post_poll_all, process_post_forward_all, MakePost, MakePostStates
from commands.prog import ProgCompiler, ProgStates, process_code, register_code_submitters, \
    register_code_submitters_with_stdin
from commands.raffle import Raffle
from commands.rate import Rate
from commands.rolls import Randoms, Rolls, process_d6, process_dice, process_mash, process_or, process_others_dice, \
    process_random, \
    process_roll, process_truth
from commands.sed import process_sed
from commands.settings import process_settings
from commands.song import process_song
from commands.stathat import StatHat
from commands.stats import Stats, StatsVPN
from commands.sticker import process_sticker, process_sticker_chat, process_animated_sticker_chat, \
    process_animated_sticker, Stickers, \
    StickerStates, process_sticker_delete
from commands.tenet import process_reverse
from commands.tesseract import process_image_to_text
from commands.tts import process_tts
from commands.tyan import Tyan
from commands.vk import process_list_vk_wall, process_vk_wall, process_vk_wall_posting, process_vk_post
from commands.votes import process_votes
from commands.weather import Weather, WeatherMap
from commands.zalgo import process_zalgo
from common.externals.fakeyou import Voices
from common.tg.filters import MetaCommand
from texts import cmd_help, cmd_start


async def process_start(message: Message):
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton(text='Добавь меня в любой чат ↩️', url='https://t.me/msu_hub_bot?startgroup=true')
    )
    return await message.reply(cmd_start, reply_markup=keyboard, disable_web_page_preview=True)


async def process_help(message: Message):
    return await message.reply(cmd_help, disable_web_page_preview=True)


async def process_cancel(message: Message, state: FSMContext):
    await state.finish()
    return await message.reply('👌🏻', reply_markup=ReplyKeyboardRemove())


async def process_raise(message: Message):
    case = random.randint(0, 4)

    if case == 0:
        await message.bot.send_message(message.chat.id + 1, 'a')
    elif case == 1:
        await message.bot.send_message(message.chat.id, 'a', reply_to_message_id=10 ** 10)
    elif case == 2:
        await message.bot.send_message(message.chat.id, 'a', parse_mode='a')
    elif case == 3:
        await message.bot.send_message(message.chat.id, 'a' * 4097)
    elif case == 4:
        await message.bot.send_message(message.chat.id, '<a')

    raise Exception(message.text or message.caption)


async def process_echo(message: Message):
    await process_json(message)
    return await message.send_copy(message.chat.id)


errors = TTLCache(256, ttl=1 * 60)


async def process_error(update: Update, error: BaseException):
    if isinstance(error, MissingIntegration):
        text = 'Эта функция пока не настроена на этом экземпляре бота.'
        if update.callback_query:
            await update.callback_query.answer(text, show_alert=True)
        elif message := (update.message or update.edited_message or update.channel_post):
            await message.reply(text)
        return True

    e = aiogram.exceptions
    error_str = redact(repr(error))
    trace = redact(''.join(traceback.format_exception(type(error), error, error.__traceback__)))
    logger.error('Update %s failed: %s\n%s', update.update_id, error_str, trace)
    if isinstance(error, (e.MessageToEditNotFound, e.BotKicked)) or (error.args and error.args[0] in {'Message was deleted', 'Replied message not found'}):
        return True
    if error_str not in errors and settings.error_chat_id:
        text = hbold('Exception') + ': ' + hpre(error_str[:1000]) + '\n' + hpre(trace[-2000:])
        await bot.send_message(settings.error_chat_id, text)
    errors[error_str] = True
    return True


async def on_startup(dp: Dispatcher):
    await app.on_startup_all()

    # Run long handlers as tasks
    run_task = False

    # Some IDs
    kek_pek_id, test_chat_id = settings.forward_chat_ids
    unrelated_id, related_chat_id = settings.related_chat_ids

    # Filters
    only_for_me = IDFilter(settings.owner_id)
    only_for_founders = IDFilter(settings.founder_ids)

    # Register handlers
    dp.register_message_handler(process_start, commands=['start'])
    dp.register_message_handler(HelpMessage.process, MetaCommand('help', 'рудз'))
    dp.register_message_handler(process_settings, MetaCommand('settings', 'настройки'))
    dp.register_callback_query_handler(HelpMessage.process_cb, HelpMessage.callback_data.filter())
    dp.register_message_handler(process_cancel, commands=['cancel'], state='*')

    dp.register_message_handler(process_donate, commands=['donate'])
    dp.register_message_handler(process_supporters, commands=['supporters'])

    dp.register_message_handler(process_links, commands=['links'])
    dp.register_message_handler(process_json, commands=['json'])
    dp.register_message_handler(process_logs, only_for_me, commands=['logs', 'log'])
    dp.register_message_handler(process_delete_after, only_for_me, commands=['delete_after'])
    dp.register_message_handler(process_beer, commands=['7uB0', 'beer'])
    dp.register_message_handler(process_pokakats, content_types=ContentType.ANY)
    dp.register_message_handler(process_puk, MetaCommand('puk', 'пук'))

    dp.register_message_handler(Weather.process, MetaCommand('weather', 'w', 'погода'))
    dp.register_message_handler(Weather.process_location, content_types=(ContentType.LOCATION, ContentType.VENUE))
    dp.register_edited_message_handler(Weather.process_location_edited, content_types=(ContentType.LOCATION, ContentType.VENUE))
    dp.register_callback_query_handler(Weather.process_cb, Weather.callback_data.filter())
    dp.register_message_handler(WeatherMap.process, commands=['map'])
    dp.register_callback_query_handler(WeatherMap.process_cb, WeatherMap.callback_data.filter())

    dp.register_message_handler(process_sticker, MetaCommand('s', 'sticker'), content_types=ContentType.ANY)
    dp.register_message_handler(process_sticker_chat, MetaCommand('sc', 'sticker_chat'), content_types=ContentType.ANY)
    dp.register_message_handler(process_animated_sticker, MetaCommand('sa'), content_types=ContentType.ANY)
    dp.register_message_handler(process_animated_sticker_chat, MetaCommand('sac'), content_types=ContentType.ANY)
    dp.register_message_handler(process_sticker_delete, MetaCommand('sd', 'sticker_delete'), content_types=ContentType.ANY)
    dp.register_message_handler(process_error_stickers, MetaCommand('error_stickers'), content_types=ContentType.ANY)
    dp.register_message_handler(Stickers.sticker_set_name, state=StickerStates.sticker_set_name, content_types=ContentType.ANY)
    dp.register_message_handler(process_punto, MetaCommand('punto', 'згтещ', 'пунто', 'geynj'), content_types=ContentType.ANY)
    dp.register_message_handler(process_transliterate, MetaCommand('trans', 'translit', 'transliterate'), content_types=ContentType.ANY)
    dp.register_message_handler(process_id, MetaCommand('id'), content_types=ContentType.ANY)
    dp.register_message_handler(process_md, MetaCommand('md', 'markdown'), content_types=ContentType.ANY)
    dp.register_message_handler(process_html, MetaCommand('html'), content_types=ContentType.ANY)
    dp.register_message_handler(process_file_id, MetaCommand('file_id', 'fi'), content_types=ContentType.ANY)
    dp.register_message_handler(process_location, MetaCommand('loc', 'location'), content_types=ContentType.ANY)
    dp.register_message_handler(Latex.process, MetaCommand('t', 'tex', 'latex'), content_types=ContentType.ANY)
    dp.register_edited_message_handler(Latex.process_edited, MetaCommand('t', 'tex', 'latex'), content_types=ContentType.ANY)
    dp.register_message_handler(process_song, MetaCommand('song', 'shazam', 'music'), content_types=ContentType.ANY)
    dp.register_message_handler(process_imgur, MetaCommand('i', 'imgur'), content_types=ContentType.ANY)

    dp.register_message_handler(process_animate, MetaCommand('a', 'animate'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_matrix, MetaCommand('m', 'matrix'), content_types=ContentType.ANY, run_task=run_task)

    register_code_submitters(dp)
    register_code_submitters_with_stdin(dp)
    dp.register_callback_query_handler(ProgCompiler.process_stdin_cb, ProgCompiler.callback_data.filter(), state='*')
    dp.register_message_handler(ProgCompiler.process_stdin_run, state=ProgStates.stdin, content_types=ContentType.ANY)
    dp.register_message_handler(process_code, MetaCommand('prog', 'pr'), content_types=ContentType.ANY)
    dp.register_message_handler(process_cheatsheet, MetaCommand('ch', 'cheat', 'cheatsheet', 'so', 'stackoverflow', 'man'), content_types=ContentType.ANY)

    dp.register_message_handler(process_lobster, MetaCommand('lobster', 'l', 'л', 'лобстер'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_demotivator, MetaCommand('demotivator', 'de', 'д', 'де'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_atmta, MetaCommand('atmta', 'атмта', 'атм', 'atm'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_atmta_v, MetaCommand('atmtav', 'атмтав', 'атмв', 'atmv'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_dalle, MetaCommand('da', 'dalle', 'да'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_stable_diffusion, MetaCommand('std', 'стд', 'сд'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_rudalle, MetaCommand('ruda', 'rudalle', 'руда'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_dalle_emoji, MetaCommand('dae', 'дае'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_latent_diffusion, MetaCommand('la', 'ла'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_toonify, MetaCommand('toonify', 'toon'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_anime, MetaCommand('an'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_drag, MetaCommand('drag'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_doll, MetaCommand('doll'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_vintage, MetaCommand('vintage'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_arcane, MetaCommand('arcane', 'arc'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_inpaint, MetaCommand('inpaint', 'remove'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_paint, MetaCommand('paint'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_inter, MetaCommand('inter'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_copilot, MetaCommand('co', 'cop', 'copilot'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_privacy, MetaCommand('privacy', 'deepprivacy', 'dp'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_which_anime, MetaCommand('anime'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_agile_gan, MetaCommand('ag', 'agilegan', 'agile_gan'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_zombie, MetaCommand('zombie', 'z'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(Tyan.process, MetaCommand('tyan', 'tyans'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_callback_query_handler(Tyan.process_cb, Tyan.callback_data.filter())
    dp.register_message_handler(process_deepdream, MetaCommand('deepdream', 'dd'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_colorizer, MetaCommand('colorizer', 'colorize', 'color'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_srgan, MetaCommand('srgan', 'sr', 'large', 'lg'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_waifu, MetaCommand('waifu', 'wa'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_demographic, MetaCommand('age', 'race', 'face', 'faces'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_what_hg, MetaCommand('wtf', 'what', 'yolo'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_nudes, MetaCommand('nudes', 'nude', 'nsfw'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_topdf, MetaCommand('pdf', 'topdf', 'to_pdf'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_bg, MetaCommand('removebg', 'bg'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_badwiki, MetaCommand('badwiki', 'bw'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_bored, MetaCommand('bored', 'bore', 'скучно', 'скука'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_porfirevich, MetaCommand('gpt2', 'гпт2'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_gpt3, MetaCommand('gpt3', 'gpt', 'гпт', 'гпт3'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_rugpt3, MetaCommand('rugpt3', 'rugpt', 'ругпт', 'ругпт3'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_openai_gpt3, MetaCommand('ogpt3', 'ogpt', 'огпт', 'огпт3'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_gptn, MetaCommand('gptn', 'gn', 'гптн', 'гн'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(Balaboba.process, MetaCommand('boba', 'боба'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_callback_query_handler(Balaboba.process_cb, Balaboba.callback_data.filter())
    dp.register_message_handler(process_duckduckgo, MetaCommand('ddg', 'wiki', 'вики', 'duckduckgo'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_ud, MetaCommand('ud', 'urban', 'slang'), content_types=ContentType.ANY, run_task=run_task)

    dp.register_message_handler(AntiBot.process, content_types=ContentType.NEW_CHAT_MEMBERS)
    dp.register_message_handler(AntiBot.process_left, content_types=ContentType.LEFT_CHAT_MEMBER)
    dp.register_callback_query_handler(AntiBot.process_cb, AntiBot.callback_data.filter())

    dp.register_message_handler(process_orfogrammka_common, MetaCommand('orfogrammka', 'orfo'), content_types=ContentType.ANY)
    dp.register_message_handler(process_orfogrammka_cicero, MetaCommand('cicero'), content_types=ContentType.ANY)
    dp.register_message_handler(process_orfogrammka_quality, MetaCommand('quality'), content_types=ContentType.ANY)

    dp.register_message_handler(process_reverse, commands=['tenet'], content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_image_to_text, MetaCommand('text', 'itt'), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_tts, MetaCommand('tts', 'speech', args=1), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_fake_voice, MetaCommand(*Voices.__members__.keys()), content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(wit.process_stt, commands=['stt'], content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(wit.process_stt, NotFilter(IDFilter(chat_id=settings.excluded_chat_id)), content_types=(ContentType.VOICE, ContentType.VIDEO_NOTE),
                                run_task=run_task)
    dp.register_message_handler(wolfram.process_wolfram, commands=['wf', 'wolfram'], content_types=ContentType.ANY, run_task=run_task)
    dp.register_message_handler(process_sed, Text(startswith=['s/', 'ы/'], ignore_case=True), content_types=ContentType.ANY, run_task=run_task)

    dp.register_message_handler(Dvach.process, Dvach.restriction_filter, commands=['2ch', 'dvach'])
    dp.register_message_handler(Dvach.process_link, Dvach.restriction_filter, Dvach.link_filter)
    dp.register_callback_query_handler(Dvach.process_cb, Dvach.callback_data.filter())

    dp.register_message_handler(process_sudo, only_for_founders, commands=['sudo'])
    dp.register_message_handler(process_revoke, only_for_founders, commands=['revoke'])
    dp.register_message_handler(process_ban, only_for_founders, commands=['ban'])
    dp.register_message_handler(process_restrict, only_for_founders, commands=['restrict', 'ro'])
    dp.register_message_handler(process_unban, only_for_founders, commands=['unban'])
    dp.register_message_handler(process_forwards, only_for_me, commands=['forwards'])

    dp.register_message_handler(process_create_infra_chat, only_for_me, commands=['create_infra_chat'])
    dp.register_message_handler(process_delete_infra_chat, only_for_me, commands=['delete_infra_chat'])
    dp.register_message_handler(process_pin, only_for_me, commands=['pin'])
    dp.register_message_handler(process_pin_all, only_for_me, commands=['pin_all'])
    dp.register_message_handler(process_update_pins, only_for_me, commands=['update_pins'])
    dp.register_message_handler(process_post_poll_all, only_for_me, commands=['post_poll_all'])
    dp.register_message_handler(process_post_all, only_for_me, commands=['post_all'])
    dp.register_message_handler(process_post_forward_all, only_for_me, commands=['post_forward_all'])
    dp.register_message_handler(MakePost.process, only_for_founders, commands=['make_post'])
    dp.register_message_handler(MakePost.process_destination, only_for_founders, state=MakePostStates.destination, content_types=ContentType.ANY)
    dp.register_message_handler(MakePost.process_waiting, only_for_founders, state=MakePostStates.waiting, content_types=ContentType.ANY)
    dp.register_message_handler(process_status, only_for_me, commands=['status'])
    dp.register_message_handler(Stats.process, MetaCommand('stats', 'meta'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Stats.process_cb, Stats.callback_data.filter())
    dp.register_message_handler(StatsVPN.process, MetaCommand('vpn'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(StatsVPN.process_cb, StatsVPN.callback_data.filter())

    dp.register_message_handler(MinecraftStatus.process, MetaCommand('mc', 'minecraft'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(MinecraftStatus.process_cb, MinecraftStatus.callback_data.filter())
    dp.register_message_handler(StatHat.process, MetaCommand('internet', 'ping', 'isdown'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(StatHat.process_cb, StatHat.callback_data.filter())
    dp.register_message_handler(Camera.process, MetaCommand('camera', 'cam'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Camera.process_cb, Camera.callback_data.filter())
    dp.register_message_handler(Debate.process, MetaCommand('debate', 'resolution'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Debate.process_cb, Debate.callback_data.filter())
    dp.register_message_handler(Crypto.process, MetaCommand('crypto', *Crypto.tickers), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Crypto.process_cb, Crypto.callback_data.filter())

    dp.register_message_handler(process_vk_wall, only_for_me, commands=['vk_wall'])
    dp.register_message_handler(process_vk_post, only_for_me, commands=['vk_post'])
    dp.register_message_handler(process_list_vk_wall, only_for_me, commands=['list_vk_wall'])

    dp.register_message_handler(process_votes, MetaCommand('vote', 'votes', 'выбираем', args=10), content_types=ContentType.ANY)
    dp.register_message_handler(Like.process, MetaCommand('like', 'likes', 'лайки'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Like.process_cb, Like.callback_data.filter())
    dp.register_message_handler(Rate.process, MetaCommand('rate', 'rates', 'рейт', 'зацените'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Rate.process_cb, Rate.callback_data.filter())
    dp.register_message_handler(process_roll, MetaCommand('roll', 'ролл'), content_types=ContentType.ANY)
    dp.register_message_handler(Rolls.process, MetaCommand('rolls', 'роллим', 'рулетка'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Rolls.process_cb, Rolls.callback_data.filter())
    dp.register_message_handler(process_random, MetaCommand('random', 'rand', 'рандом'), content_types=ContentType.ANY)
    dp.register_message_handler(Randoms.process, MetaCommand('randoms', 'рандомим', 'числа'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Randoms.process_cb, Randoms.callback_data.filter())
    dp.register_message_handler(Raffle.process, MetaCommand('raffle', 'розыгрыш', 'конкурс'), content_types=ContentType.ANY)
    dp.register_callback_query_handler(Raffle.process_cb, Raffle.callback_data.filter())
    dp.register_message_handler(process_truth, MetaCommand('truth', 'истина', 'истину', 'истины'), content_types=ContentType.ANY)
    dp.register_message_handler(process_or, MetaCommand('or'), content_types=ContentType.ANY)
    dp.register_message_handler(process_mash, MetaCommand('mash'), content_types=ContentType.ANY)
    dp.register_message_handler(process_d6, MetaCommand('d6'), content_types=ContentType.ANY)
    dp.register_message_handler(process_dice, MetaCommand('dice'), content_types=ContentType.ANY)
    dp.register_message_handler(process_others_dice, content_types=ContentType.DICE)
    dp.register_message_handler(process_zalgo, MetaCommand('zalgo'))
    dp.register_message_handler(process_figlet, MetaCommand('figlet'))
    dp.register_message_handler(process_excuse, MetaCommand('excuse', 'e'))
    dp.register_message_handler(process_gender, MetaCommand('gender', 'g'))
    dp.register_message_handler(Geoguess.process, MetaCommand('geoguess'))
    dp.register_message_handler(Geoguess.top, MetaCommand('geoguess_top'))
    dp.register_callback_query_handler(Geoguess.process_cb, Geoguess.callback_data.filter())
    dp.register_message_handler(process_me, MetaCommand('me'))
    dp.register_message_handler(process_copy, MetaCommand('copy', 'see', 'uncover'))
    dp.register_message_handler(process_arxiv, commands=['arxiv'])

    dp.register_message_handler(process_translate, MetaCommand('translate', 'tr', args=2))
    dp.register_message_handler(process_en, MetaCommand('en'))
    dp.register_message_handler(process_ru, MetaCommand('ru'))
    dp.register_message_handler(process_langs, MetaCommand('langs'))

    dp.register_message_handler(process_sanya, MetaCommand('sanya', 'саня'))
    dp.register_message_handler(process_dyubs, MetaCommand('dyubs', 'дюбс'))
    dp.register_message_handler(process_popov, MetaCommand('popov', 'попов'))

    dp.register_channel_post_handler(process_forward_builder(test_chat_id), IDFilter(chat_id=kek_pek_id), content_types=ContentType.ANY)
    dp.register_channel_post_handler(process_forward_builder(related_chat_id), IDFilter(chat_id=unrelated_id), content_types=ContentType.ANY)
    dp.register_channel_post_handler(process_pookie_pook, IDFilter(chat_id=settings.pookie_chat_id), content_types=ContentType.ANY)

    dp.register_inline_handler(process_inline)

    dp.register_message_handler(process_echo, IDFilter(chat_id=settings.echo_chat_id), content_types=ContentType.ANY)
    dp.register_message_handler(process_raise, only_for_me, MetaCommand('raise'), content_types=ContentType.ANY)

    dp.register_errors_handler(process_error)

    if not os.getenv('DEBUG_MODE') and False:
        app.scheduler.add_job(process_vk_wall_posting, 'interval', seconds=1 * 60)
    app.scheduler.add_job(redis.process_messages_to_delete, 'interval', seconds=1 * 60, args=[bot])
    app.scheduler.start()

    logger.info('Bot started!')


async def on_shutdown(_dp: Dispatcher):
    await Geoguess.shutdown()
    await app.on_shutdown_all()


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=False, on_startup=on_startup, on_shutdown=on_shutdown, timeout=60)
