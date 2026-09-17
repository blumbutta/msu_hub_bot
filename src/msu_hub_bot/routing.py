"""Ordered feature-router fragments preserve cross-command first-match behavior."""

from aiogram import F, Router
from aiogram.enums import ContentType
from aiogram.filters import StateFilter
from msu_hub_bot.telegram.filters import MetaCommand, SlashCommand
from msu_hub_bot.telegram.state import UpdateStateContext
from msu_hub_bot.settings import Settings
from msu_hub_bot.providers.wit import Wit
from msu_hub_bot.providers.wolfram import WolframAPI
from msu_hub_bot.commands.control import process_start, process_cancel, process_echo, process_error, process_expired_callback
from msu_hub_bot.commands.admin import (
    process_ban,
    process_forward_builder,
    process_forwards,
    process_restrict,
    process_revoke,
    process_sudo,
    process_unban,
)
from msu_hub_bot.commands.animate import process_animate, process_matrix
from msu_hub_bot.commands.antibot import AntiBot
from msu_hub_bot.commands.arxiv import process_arxiv
from msu_hub_bot.commands.camera import Camera
from msu_hub_bot.commands.crypto import Crypto
from msu_hub_bot.commands.debate import Debate
from msu_hub_bot.commands.debug import process_delete_after, process_json, process_logs
from msu_hub_bot.commands.dvach import Dvach
from msu_hub_bot.commands.errors import process_error_stickers, process_donate, process_supporters
from msu_hub_bot.commands.excuses import process_excuse
from msu_hub_bot.commands.externals import (
    process_bg,
    process_porfirevich,
    process_topdf,
    process_imgur,
    process_duckduckgo,
    process_which_anime,
    process_ud,
)
from msu_hub_bot.commands.figlet import process_figlet
from msu_hub_bot.commands.fun import matches_pokakats, process_beer, process_pokakats, process_puk
from msu_hub_bot.commands.genders import process_gender
from msu_hub_bot.commands.geoguess import Geoguess
from msu_hub_bot.commands.chess import Chess
from msu_hub_bot.commands.help import HelpMessage
from msu_hub_bot.commands.infra import (
    process_create_infra_chat,
    process_delete_infra_chat,
    process_inline,
    process_links,
    process_pin,
    process_pin_all,
    process_status,
    process_update_pins,
)
from msu_hub_bot.commands.latex import Latex
from msu_hub_bot.commands.likes import Like
from msu_hub_bot.commands.lingvanex import process_langs, process_translate, process_en, process_ru
from msu_hub_bot.commands.lobster import process_lobster, process_demotivator, process_atmta, process_atmta_v
from msu_hub_bot.commands.location import process_location
from msu_hub_bot.commands.minecraft import MinecraftStatus
from msu_hub_bot.commands.other import (
    process_me,
    process_transliterate,
    process_punto,
    process_md,
    process_html,
    process_id,
    process_copy,
    process_file_id,
)
from msu_hub_bot.commands.personal import process_sanya, process_dyubs, process_pookie_pook, process_popov
from msu_hub_bot.commands.posting import process_post_all, process_post_forward_all, MakePost, MakePostStates
from msu_hub_bot.commands.prog import ProgCompiler, ProgStates, process_code, register_code_submitters, register_code_submitters_with_stdin
from msu_hub_bot.commands.raffle import Raffle
from msu_hub_bot.commands.rate import Rate
from msu_hub_bot.commands.rolls import (
    Randoms,
    Rolls,
    process_d6,
    process_dice,
    process_mash,
    process_or,
    process_others_dice,
    process_random,
    process_roll,
    process_truth,
)
from msu_hub_bot.commands.sed import process_sed
from msu_hub_bot.commands.settings import process_settings
from msu_hub_bot.commands.song import process_song
from msu_hub_bot.commands.stats import Stats
from msu_hub_bot.commands.sticker import (
    process_sticker,
    process_sticker_chat,
    process_animated_sticker_chat,
    process_animated_sticker,
    Stickers,
    StickerStates,
    process_sticker_delete,
)
from msu_hub_bot.commands.tenet import process_reverse
from msu_hub_bot.commands.tesseract import process_image_to_text
from msu_hub_bot.commands.tts import process_tts
from msu_hub_bot.commands.tyan import Tyan
from msu_hub_bot.commands.vk import process_list_vk_wall, process_vk_wall, process_vk_post
from msu_hub_bot.commands.votes import process_votes
from msu_hub_bot.commands.weather import Weather, WeatherMap
from msu_hub_bot.commands.zalgo import process_zalgo


def fsm_callback_allowed(query: object, state_context: UpdateStateContext) -> bool:
    return state_context.eligible


def build_router(*, wit: Wit, wolfram: WolframAPI, config: Settings) -> Router:
    settings = config
    root = Router(name="hub")
    current: Router | None = None
    domain = ""

    def group(name: str) -> Router:
        nonlocal current, domain
        if current is None or name != domain:
            current = Router(name=f"{len(root.sub_routers)}:{name}")
            root.include_router(current)
            domain = name
        return current

    kek_pek_id, test_chat_id = settings.forward_chat_ids
    unrelated_id, related_chat_id = settings.related_chat_ids
    only_for_me = F.from_user.id == settings.owner_id
    only_for_founders = F.from_user.id.in_(settings.founder_ids)
    group("control").message.register(
        process_start,
        SlashCommand("start"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_start", "fsm_release": True},
    )
    group("help").message.register(
        HelpMessage.process,
        MetaCommand("help", "рудз"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "HelpMessage.process", "fsm_release": True},
    )
    group("settings").message.register(
        process_settings,
        MetaCommand("settings", "настройки"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_settings", "fsm_release": True},
    )
    group("help").callback_query.register(
        HelpMessage.process_cb,
        HelpMessage.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "HelpMessage.process_cb", "fsm_release": True},
    )
    group("control").message.register(
        process_cancel,
        SlashCommand("cancel"),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_cancel", "fsm_release": False},
    )
    group("errors").message.register(
        process_donate,
        SlashCommand("donate"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_donate", "fsm_release": True},
    )
    group("errors").message.register(
        process_supporters,
        SlashCommand("supporters"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_supporters", "fsm_release": True},
    )
    group("infra").message.register(
        process_links,
        SlashCommand("links"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_links", "fsm_release": True},
    )
    group("debug").message.register(
        process_json,
        SlashCommand("json"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_json", "fsm_release": True},
    )
    group("debug").message.register(
        process_logs,
        only_for_me,
        SlashCommand("logs", "log"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_logs", "fsm_release": True},
    )
    group("debug").message.register(
        process_delete_after,
        only_for_me,
        SlashCommand("delete_after"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_delete_after", "fsm_release": True},
    )
    group("fun").message.register(
        process_beer,
        SlashCommand("7uB0", "beer"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_beer", "fsm_release": True},
    )
    group("fun").message.register(
        process_pokakats, matches_pokakats, StateFilter(None), flags={"handler_key": "process_pokakats", "fsm_release": True}
    )
    group("fun").message.register(
        process_puk,
        MetaCommand("puk", "пук"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_puk", "fsm_release": True},
    )
    group("weather").message.register(
        Weather.process,
        MetaCommand("weather", "w", "погода"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "Weather.process", "fsm_release": True},
    )
    group("weather").message.register(
        Weather.process_location,
        StateFilter(None),
        F.content_type.in_((ContentType.LOCATION, ContentType.VENUE)),
        flags={"handler_key": "Weather.process_location", "fsm_release": True},
    )
    group("weather").edited_message.register(
        Weather.process_location_edited,
        StateFilter(None),
        F.content_type.in_((ContentType.LOCATION, ContentType.VENUE)),
        flags={"handler_key": "Weather.process_location_edited", "fsm_release": True},
    )
    group("weather").callback_query.register(
        Weather.process_cb,
        Weather.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "Weather.process_cb", "fsm_release": True},
    )
    group("weather").message.register(
        WeatherMap.process,
        SlashCommand("map"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "WeatherMap.process", "fsm_release": True},
    )
    group("weather").callback_query.register(
        WeatherMap.process_cb,
        WeatherMap.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "WeatherMap.process_cb", "fsm_release": True},
    )
    group("sticker").message.register(
        process_sticker, MetaCommand("s", "sticker"), StateFilter(None), flags={"handler_key": "process_sticker", "fsm_release": False}
    )
    group("sticker").message.register(
        process_sticker_chat,
        MetaCommand("sc", "sticker_chat"),
        StateFilter(None),
        flags={"handler_key": "process_sticker_chat", "fsm_release": False},
    )
    group("sticker").message.register(
        process_animated_sticker,
        MetaCommand("sa"),
        StateFilter(None),
        flags={"handler_key": "process_animated_sticker", "fsm_release": False},
    )
    group("sticker").message.register(
        process_animated_sticker_chat,
        MetaCommand("sac"),
        StateFilter(None),
        flags={"handler_key": "process_animated_sticker_chat", "fsm_release": False},
    )
    group("sticker").message.register(
        process_sticker_delete,
        MetaCommand("sd", "sticker_delete"),
        StateFilter(None),
        flags={"handler_key": "process_sticker_delete", "fsm_release": False},
    )
    group("errors").message.register(
        process_error_stickers,
        MetaCommand("error_stickers"),
        StateFilter(None),
        flags={"handler_key": "process_error_stickers", "fsm_release": True},
    )
    group("sticker").message.register(
        Stickers.sticker_set_name,
        StateFilter(StickerStates.sticker_set_name),
        flags={"handler_key": "Stickers.sticker_set_name", "fsm_release": False},
    )
    group("other").message.register(
        process_punto,
        MetaCommand("punto", "згтещ", "пунто", "geynj"),
        StateFilter(None),
        flags={"handler_key": "process_punto", "fsm_release": True},
    )
    group("other").message.register(
        process_transliterate,
        MetaCommand("trans", "translit", "transliterate"),
        StateFilter(None),
        flags={"handler_key": "process_transliterate", "fsm_release": True},
    )
    group("other").message.register(
        process_id, MetaCommand("id"), StateFilter(None), flags={"handler_key": "process_id", "fsm_release": True}
    )
    group("other").message.register(
        process_md, MetaCommand("md", "markdown"), StateFilter(None), flags={"handler_key": "process_md", "fsm_release": True}
    )
    group("other").message.register(
        process_html, MetaCommand("html"), StateFilter(None), flags={"handler_key": "process_html", "fsm_release": True}
    )
    group("other").message.register(
        process_file_id, MetaCommand("file_id", "fi"), StateFilter(None), flags={"handler_key": "process_file_id", "fsm_release": True}
    )
    group("location").message.register(
        process_location, MetaCommand("loc", "location"), StateFilter(None), flags={"handler_key": "process_location", "fsm_release": True}
    )
    group("latex").message.register(
        Latex.process, MetaCommand("t", "tex", "latex"), StateFilter(None), flags={"handler_key": "Latex.process", "fsm_release": True}
    )
    group("latex").edited_message.register(
        Latex.process_edited,
        MetaCommand("t", "tex", "latex"),
        StateFilter(None),
        flags={"handler_key": "Latex.process_edited", "fsm_release": True},
    )
    group("song").message.register(
        process_song, MetaCommand("song", "shazam", "music"), StateFilter(None), flags={"handler_key": "process_song", "fsm_release": True}
    )
    group("externals").message.register(
        process_imgur, MetaCommand("i", "imgur"), StateFilter(None), flags={"handler_key": "process_imgur", "fsm_release": True}
    )
    group("animate").message.register(
        process_animate, MetaCommand("animate"), StateFilter(None), flags={"handler_key": "process_animate", "fsm_release": True}
    )
    group("animate").message.register(
        process_matrix, MetaCommand("matrix"), StateFilter(None), flags={"handler_key": "process_matrix", "fsm_release": True}
    )
    register_code_submitters(group("code"))
    register_code_submitters_with_stdin(group("code"))
    group("prog").callback_query.register(
        ProgCompiler.process_stdin_cb,
        fsm_callback_allowed,
        ProgCompiler.callback_data.filter(),
        flags={"handler_key": "ProgCompiler.process_stdin_cb", "fsm_release": False},
    )
    group("prog").message.register(
        ProgCompiler.process_stdin_run,
        StateFilter(ProgStates.stdin),
        flags={"handler_key": "ProgCompiler.process_stdin_run", "fsm_release": False},
    )
    group("prog").message.register(
        process_code, MetaCommand("prog", "pr"), StateFilter(None), flags={"handler_key": "process_code", "fsm_release": True}
    )
    group("lobster").message.register(
        process_lobster,
        MetaCommand("lobster", "l", "л", "лобстер"),
        StateFilter(None),
        flags={"handler_key": "process_lobster", "fsm_release": True},
    )
    group("lobster").message.register(
        process_demotivator,
        MetaCommand("demotivator", "de", "д", "де"),
        StateFilter(None),
        flags={"handler_key": "process_demotivator", "fsm_release": True},
    )
    group("lobster").message.register(
        process_atmta,
        MetaCommand("atmta", "атмта", "атм", "atm"),
        StateFilter(None),
        flags={"handler_key": "process_atmta", "fsm_release": True},
    )
    group("lobster").message.register(
        process_atmta_v,
        MetaCommand("atmtav", "атмтав", "атмв", "atmv"),
        StateFilter(None),
        flags={"handler_key": "process_atmta_v", "fsm_release": True},
    )
    group("externals").message.register(
        process_which_anime, MetaCommand("anime"), StateFilter(None), flags={"handler_key": "process_which_anime", "fsm_release": True}
    )
    group("tyan").message.register(
        Tyan.process, MetaCommand("tyan", "tyans"), StateFilter(None), flags={"handler_key": "Tyan.process", "fsm_release": True}
    )
    group("tyan").callback_query.register(
        Tyan.process_cb, Tyan.callback_data.filter(), StateFilter(None), flags={"handler_key": "Tyan.process_cb", "fsm_release": True}
    )
    group("externals").message.register(
        process_topdf, MetaCommand("pdf", "topdf", "to_pdf"), StateFilter(None), flags={"handler_key": "process_topdf", "fsm_release": True}
    )
    group("externals").message.register(
        process_bg, MetaCommand("removebg", "bg"), StateFilter(None), flags={"handler_key": "process_bg", "fsm_release": True}
    )
    group("externals").message.register(
        process_porfirevich,
        MetaCommand("gpt2", "гпт2"),
        StateFilter(None),
        flags={"handler_key": "process_porfirevich", "fsm_release": True},
    )
    group("externals").message.register(
        process_duckduckgo,
        MetaCommand("ddg", "wiki", "вики", "duckduckgo"),
        StateFilter(None),
        flags={"handler_key": "process_duckduckgo", "fsm_release": True},
    )
    group("externals").message.register(
        process_ud, MetaCommand("ud", "urban", "slang"), StateFilter(None), flags={"handler_key": "process_ud", "fsm_release": True}
    )
    group("antibot").message.register(
        AntiBot.process,
        StateFilter(None),
        F.content_type == ContentType.NEW_CHAT_MEMBERS,
        flags={"handler_key": "AntiBot.process", "fsm_release": True},
    )
    group("antibot").message.register(
        AntiBot.process_left,
        StateFilter(None),
        F.content_type == ContentType.LEFT_CHAT_MEMBER,
        flags={"handler_key": "AntiBot.process_left", "fsm_release": True},
    )
    group("antibot").callback_query.register(
        AntiBot.process_cb,
        AntiBot.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "AntiBot.process_cb", "fsm_release": True},
    )
    group("tenet").message.register(
        process_reverse, SlashCommand("tenet"), StateFilter(None), flags={"handler_key": "process_reverse", "fsm_release": True}
    )
    group("tesseract").message.register(
        process_image_to_text,
        MetaCommand("text", "itt"),
        StateFilter(None),
        flags={"handler_key": "process_image_to_text", "fsm_release": True},
    )
    group("tts").message.register(
        process_tts, MetaCommand("tts", "speech", args=1), StateFilter(None), flags={"handler_key": "process_tts", "fsm_release": True}
    )
    group("speech").message.register(
        wit.process_stt_command,
        SlashCommand("stt"),
        StateFilter(None),
        flags={"handler_key": "wit.process_stt_command", "fsm_release": True},
    )
    group("speech").message.register(
        wit.process_stt,
        F.chat.id != settings.excluded_chat_id,
        StateFilter(None),
        F.content_type.in_((ContentType.VOICE, ContentType.VIDEO_NOTE)),
        flags={"handler_key": "wit.process_stt", "fsm_release": True},
    )
    group("wolfram").message.register(
        wolfram.process_wolfram,
        SlashCommand("wf", "wolfram"),
        StateFilter(None),
        flags={"handler_key": "wolfram.process_wolfram", "fsm_release": True},
    )
    group("sed").message.register(
        process_sed, F.text.lower().startswith(("s/", "ы/")), StateFilter(None), flags={"handler_key": "process_sed", "fsm_release": True}
    )
    group("dvach").message.register(
        Dvach.process,
        Dvach.restriction_filter,
        SlashCommand("2ch", "dvach"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "Dvach.process", "fsm_release": True},
    )
    group("dvach").message.register(
        Dvach.process_link,
        Dvach.restriction_filter,
        Dvach.link_filter,
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "Dvach.process_link", "fsm_release": True},
    )
    group("dvach").callback_query.register(
        Dvach.process_cb, Dvach.callback_data.filter(), StateFilter(None), flags={"handler_key": "Dvach.process_cb", "fsm_release": True}
    )
    group("admin").message.register(
        process_sudo,
        only_for_founders,
        SlashCommand("sudo"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_sudo", "fsm_release": True},
    )
    group("admin").message.register(
        process_revoke,
        only_for_founders,
        SlashCommand("revoke"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_revoke", "fsm_release": True},
    )
    group("admin").message.register(
        process_ban,
        only_for_founders,
        SlashCommand("ban"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_ban", "fsm_release": True},
    )
    group("admin").message.register(
        process_restrict,
        only_for_founders,
        SlashCommand("restrict", "ro"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_restrict", "fsm_release": True},
    )
    group("admin").message.register(
        process_unban,
        only_for_founders,
        SlashCommand("unban"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_unban", "fsm_release": True},
    )
    group("admin").message.register(
        process_forwards,
        only_for_me,
        SlashCommand("forwards"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_forwards", "fsm_release": True},
    )
    group("infra").message.register(
        process_create_infra_chat,
        only_for_me,
        SlashCommand("create_infra_chat"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_create_infra_chat", "fsm_release": True},
    )
    group("infra").message.register(
        process_delete_infra_chat,
        only_for_me,
        SlashCommand("delete_infra_chat"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_delete_infra_chat", "fsm_release": True},
    )
    group("infra").message.register(
        process_pin,
        only_for_me,
        SlashCommand("pin"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_pin", "fsm_release": True},
    )
    group("infra").message.register(
        process_pin_all,
        only_for_me,
        SlashCommand("pin_all"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_pin_all", "fsm_release": True},
    )
    group("infra").message.register(
        process_update_pins,
        only_for_me,
        SlashCommand("update_pins"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_update_pins", "fsm_release": True},
    )
    group("posting").message.register(
        process_post_all,
        only_for_me,
        SlashCommand("post_all"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_post_all", "fsm_release": True},
    )
    group("posting").message.register(
        process_post_forward_all,
        only_for_me,
        SlashCommand("post_forward_all"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_post_forward_all", "fsm_release": True},
    )
    group("posting").message.register(
        MakePost.process,
        only_for_founders,
        SlashCommand("make_post"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "MakePost.process", "fsm_release": False},
    )
    group("posting").message.register(
        MakePost.process_destination,
        only_for_founders,
        StateFilter(MakePostStates.destination),
        flags={"handler_key": "MakePost.process_destination", "fsm_release": False},
    )
    group("posting").message.register(
        MakePost.process_waiting,
        only_for_founders,
        StateFilter(MakePostStates.waiting),
        flags={"handler_key": "MakePost.process_waiting", "fsm_release": False},
    )
    group("infra").message.register(
        process_status,
        only_for_me,
        SlashCommand("status"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_status", "fsm_release": True},
    )
    group("stats").message.register(
        Stats.process, MetaCommand("stats", "meta"), StateFilter(None), flags={"handler_key": "Stats.process", "fsm_release": True}
    )
    group("stats").callback_query.register(
        Stats.process_cb, Stats.callback_data.filter(), StateFilter(None), flags={"handler_key": "Stats.process_cb", "fsm_release": True}
    )
    group("minecraft").message.register(
        MinecraftStatus.process,
        MetaCommand("mc", "minecraft"),
        StateFilter(None),
        flags={"handler_key": "MinecraftStatus.process", "fsm_release": True},
    )
    group("minecraft").callback_query.register(
        MinecraftStatus.process_cb,
        MinecraftStatus.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "MinecraftStatus.process_cb", "fsm_release": True},
    )
    group("camera").message.register(
        Camera.process, MetaCommand("camera", "cam"), StateFilter(None), flags={"handler_key": "Camera.process", "fsm_release": True}
    )
    group("camera").callback_query.register(
        Camera.process_cb, Camera.callback_data.filter(), StateFilter(None), flags={"handler_key": "Camera.process_cb", "fsm_release": True}
    )
    group("debate").message.register(
        Debate.process, MetaCommand("debate", "resolution"), StateFilter(None), flags={"handler_key": "Debate.process", "fsm_release": True}
    )
    group("debate").callback_query.register(
        Debate.process_cb, Debate.callback_data.filter(), StateFilter(None), flags={"handler_key": "Debate.process_cb", "fsm_release": True}
    )
    group("crypto").message.register(
        Crypto.process,
        MetaCommand("crypto", *Crypto.tickers),
        StateFilter(None),
        flags={"handler_key": "Crypto.process", "fsm_release": True},
    )
    group("crypto").callback_query.register(
        Crypto.process_cb, Crypto.callback_data.filter(), StateFilter(None), flags={"handler_key": "Crypto.process_cb", "fsm_release": True}
    )
    group("vk").message.register(
        process_vk_wall,
        only_for_me,
        SlashCommand("vk_wall"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_vk_wall", "fsm_release": True},
    )
    group("vk").message.register(
        process_vk_post,
        only_for_me,
        SlashCommand("vk_post"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_vk_post", "fsm_release": True},
    )
    group("vk").message.register(
        process_list_vk_wall,
        only_for_me,
        SlashCommand("list_vk_wall"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_list_vk_wall", "fsm_release": True},
    )
    group("votes").message.register(
        process_votes,
        MetaCommand("vote", "votes", "выбираем", args=10),
        StateFilter(None),
        flags={"handler_key": "process_votes", "fsm_release": True},
    )
    group("likes").message.register(
        Like.process, MetaCommand("like", "likes", "лайки"), StateFilter(None), flags={"handler_key": "Like.process", "fsm_release": True}
    )
    group("likes").callback_query.register(
        Like.process_cb, Like.callback_data.filter(), StateFilter(None), flags={"handler_key": "Like.process_cb", "fsm_release": True}
    )
    group("rate").message.register(
        Rate.process,
        MetaCommand("rate", "rates", "рейт", "зацените"),
        StateFilter(None),
        flags={"handler_key": "Rate.process", "fsm_release": True},
    )
    group("rate").callback_query.register(
        Rate.process_cb, Rate.callback_data.filter(), StateFilter(None), flags={"handler_key": "Rate.process_cb", "fsm_release": True}
    )
    group("rolls").message.register(
        process_roll, MetaCommand("roll", "ролл"), StateFilter(None), flags={"handler_key": "process_roll", "fsm_release": True}
    )
    group("rolls").message.register(
        Rolls.process,
        MetaCommand("rolls", "роллим", "рулетка"),
        StateFilter(None),
        flags={"handler_key": "Rolls.process", "fsm_release": True},
    )
    group("rolls").callback_query.register(
        Rolls.process_cb, Rolls.callback_data.filter(), StateFilter(None), flags={"handler_key": "Rolls.process_cb", "fsm_release": True}
    )
    group("rolls").message.register(
        process_random,
        MetaCommand("random", "rand", "рандом"),
        StateFilter(None),
        flags={"handler_key": "process_random", "fsm_release": True},
    )
    group("rolls").message.register(
        Randoms.process,
        MetaCommand("randoms", "рандомим", "числа"),
        StateFilter(None),
        flags={"handler_key": "Randoms.process", "fsm_release": True},
    )
    group("rolls").callback_query.register(
        Randoms.process_cb,
        Randoms.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "Randoms.process_cb", "fsm_release": True},
    )
    group("raffle").message.register(
        Raffle.process,
        MetaCommand("raffle", "розыгрыш", "конкурс"),
        StateFilter(None),
        flags={"handler_key": "Raffle.process", "fsm_release": True},
    )
    group("raffle").callback_query.register(
        Raffle.process_cb, Raffle.callback_data.filter(), StateFilter(None), flags={"handler_key": "Raffle.process_cb", "fsm_release": True}
    )
    group("rolls").message.register(
        process_truth,
        MetaCommand("truth", "истина", "истину", "истины"),
        StateFilter(None),
        flags={"handler_key": "process_truth", "fsm_release": True},
    )
    group("rolls").message.register(
        process_or, MetaCommand("or"), StateFilter(None), flags={"handler_key": "process_or", "fsm_release": True}
    )
    group("rolls").message.register(
        process_mash, MetaCommand("mash"), StateFilter(None), flags={"handler_key": "process_mash", "fsm_release": True}
    )
    group("rolls").message.register(
        process_d6, MetaCommand("d6"), StateFilter(None), flags={"handler_key": "process_d6", "fsm_release": True}
    )
    group("rolls").message.register(
        process_dice, MetaCommand("dice"), StateFilter(None), flags={"handler_key": "process_dice", "fsm_release": True}
    )
    group("rolls").message.register(
        process_others_dice,
        StateFilter(None),
        F.content_type == ContentType.DICE,
        flags={"handler_key": "process_others_dice", "fsm_release": True},
    )
    group("zalgo").message.register(
        process_zalgo,
        MetaCommand("zalgo"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_zalgo", "fsm_release": True},
    )
    group("figlet").message.register(
        process_figlet,
        MetaCommand("figlet"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_figlet", "fsm_release": True},
    )
    group("excuses").message.register(
        process_excuse,
        MetaCommand("excuse", "e"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_excuse", "fsm_release": True},
    )
    group("genders").message.register(
        process_gender,
        MetaCommand("gender", "g"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_gender", "fsm_release": True},
    )
    group("chess").message.register(
        Chess.process,
        MetaCommand("chess"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "Chess.process", "fsm_release": True},
    )
    group("chess").message.register(
        Chess.top,
        MetaCommand("chess_top"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "Chess.top", "fsm_release": True},
    )
    group("chess").callback_query.register(
        Chess.process_cb,
        Chess.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "Chess.process_cb", "fsm_release": True},
    )
    group("geoguess").message.register(
        Geoguess.process,
        MetaCommand("geoguess"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "Geoguess.process", "fsm_release": True},
    )
    group("geoguess").message.register(
        Geoguess.top,
        MetaCommand("geoguess_top"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "Geoguess.top", "fsm_release": True},
    )
    group("geoguess").callback_query.register(
        Geoguess.process_cb,
        Geoguess.callback_data.filter(),
        StateFilter(None),
        flags={"handler_key": "Geoguess.process_cb", "fsm_release": True},
    )
    group("other").message.register(
        process_me,
        MetaCommand("me"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_me", "fsm_release": True},
    )
    group("other").message.register(
        process_copy,
        MetaCommand("copy", "see", "uncover"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_copy", "fsm_release": True},
    )
    group("arxiv").message.register(
        process_arxiv,
        SlashCommand("arxiv"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_arxiv", "fsm_release": True},
    )
    group("lingvanex").message.register(
        process_translate,
        MetaCommand("translate", "tr", args=2),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_translate", "fsm_release": True},
    )
    group("lingvanex").message.register(
        process_en,
        MetaCommand("en"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_en", "fsm_release": True},
    )
    group("lingvanex").message.register(
        process_ru,
        MetaCommand("ru"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_ru", "fsm_release": True},
    )
    group("lingvanex").message.register(
        process_langs,
        MetaCommand("langs"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_langs", "fsm_release": True},
    )
    group("personal").message.register(
        process_sanya,
        MetaCommand("sanya", "саня"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_sanya", "fsm_release": True},
    )
    group("personal").message.register(
        process_dyubs,
        MetaCommand("dyubs", "дюбс"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_dyubs", "fsm_release": True},
    )
    group("personal").message.register(
        process_popov,
        MetaCommand("popov", "попов"),
        StateFilter(None),
        F.content_type == ContentType.TEXT,
        flags={"handler_key": "process_popov", "fsm_release": True},
    )
    group("admin").channel_post.register(
        process_forward_builder(test_chat_id),
        F.chat.id == kek_pek_id,
        StateFilter(None),
        flags={"handler_key": "process_forward_builder", "fsm_release": True},
    )
    group("admin").channel_post.register(
        process_forward_builder(related_chat_id),
        F.chat.id == unrelated_id,
        StateFilter(None),
        flags={"handler_key": "process_forward_builder", "fsm_release": True},
    )
    group("personal").channel_post.register(
        process_pookie_pook,
        F.chat.id == settings.pookie_chat_id,
        StateFilter(None),
        flags={"handler_key": "process_pookie_pook", "fsm_release": True},
    )
    group("infra").inline_query.register(process_inline, flags={"handler_key": "process_inline", "fsm_release": True})
    group("control").message.register(
        process_echo, F.chat.id == settings.echo_chat_id, StateFilter(None), flags={"handler_key": "process_echo", "fsm_release": True}
    )
    group("control").callback_query.register(
        process_expired_callback, flags={"handler_key": "process_expired_callback", "fsm_release": True}
    )
    group("control").errors.register(process_error, flags={"handler_key": "process_error", "fsm_release": True})
    return root
