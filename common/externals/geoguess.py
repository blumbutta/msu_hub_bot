"""Random geotagged Commons photos. No media downloads or local cache."""
import asyncio
import math
import random
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp

from common.externals.exceptions import ExternalServiceError

# Inland search points with small radii keep the answer unambiguous.
PLACES = (
    ('Норвегия', 'Берген', 60.397, 5.325),
    ('Португалия', 'Лиссабон', 38.711, -9.130),
    ('Япония', 'Киото', 35.003, 135.778),
    ('Франция', 'Париж', 48.8584, 2.2945),
    ('Италия', 'Рим', 41.8902, 12.4922),
    ('Испания', 'Севилья', 37.386, -5.993),
    ('Германия', 'Мюнхен', 48.1372, 11.5756),
    ('Чехия', 'Прага', 50.0875, 14.4213),
    ('Польша', 'Краков', 50.0614, 19.9366),
    ('Венгрия', 'Будапешт', 47.4979, 19.0402),
    ('Греция', 'Афины', 37.9715, 23.7257),
    ('Турция', 'Стамбул', 41.0086, 28.9802),
    ('Великобритания', 'Эдинбург', 55.9486, -3.1999),
    ('Швеция', 'Стокгольм', 59.325, 18.071),
    ('Финляндия', 'Хельсинки', 60.169, 24.952),
    ('США', 'Чикаго', 41.8826, -87.6226),
    ('Канада', 'Монреаль', 45.504, -73.556),
    ('Мексика', 'Мехико', 19.4326, -99.1332),
    ('Бразилия', 'Рио-де-Жанейро', -22.9519, -43.2105),
    ('Австралия', 'Сидней', -33.8568, 151.2153),
)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value: str, limit: int) -> str:
    parser = PlainText()
    parser.feed(value)
    return ' '.join(' '.join(parser.parts).split())[:limit]


def safe_url(value, hosts):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == 'https' and parsed.netloc in hosts


@dataclass(frozen=True)
class Photo:
    country: str
    city: str
    url: str
    source: str
    author: str
    license: str
    license_url: str


def candidates(data, place):
    country, city, lat, lon = place
    photos = []
    for page in data.get('query', {}).get('pages', {}).values():
        try:
            info = page['imageinfo'][0]
            metadata = info['extmetadata']
            def value(key):
                return metadata[key]['value']
            if info['mime'] != 'image/jpeg' or min(info['width'], info['height']) < 600:
                continue
            latitude, longitude = float(value('GPSLatitude')), float(value('GPSLongitude'))
            distance = math.hypot((latitude - lat) * 111320, (longitude - lon) * 111320 * math.cos(math.radians(lat)))
            if not math.isfinite(distance) or distance > 1000:
                continue
            url = info.get('thumburl', info['url'])
            license_url = value('LicenseUrl').replace('http://', 'https://', 1)
            if not safe_url(url, {'upload.wikimedia.org', 'thumb.wikimedia.org'}):
                continue
            if not safe_url(license_url, {'creativecommons.org'}):
                continue
            pageid = int(page['pageid'])
            author = plain(value('Artist'), 90)
            license_name = plain(value('LicenseShortName'), 40)
            if not author or not license_name or pageid <= 0:
                continue
            photos.append(Photo(country, city, url, f'https://commons.wikimedia.org/?curid={pageid}', author, license_name, license_url))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return photos


async def random_photo() -> Photo:
    place = random.choice(PLACES)
    params = {
        'action': 'query', 'generator': 'geosearch', 'ggscoord': f'{place[2]}|{place[3]}',
        'ggsradius': 700, 'ggsnamespace': 6, 'ggslimit': 20,
        'prop': 'imageinfo', 'iiprop': 'url|extmetadata|mime|size',
        'iiextmetadatafilter': 'Artist|LicenseShortName|LicenseUrl|GPSLatitude|GPSLongitude',
        'iiurlwidth': 960, 'format': 'json',
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={'User-Agent': 'MSUHubBot-Geoguess/1.0 (https://github.com/uburuntu/msu_hub_bot)'},
        ) as session:
            async with session.get('https://commons.wikimedia.org/w/api.php', params=params, allow_redirects=False) as response:
                if response.status != 200:
                    raise ExternalServiceError('Источник фотографий сейчас недоступен. Попробуй позже.')
                data = await response.json()
        photos = candidates(data, place)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError) as exc:
        raise ExternalServiceError('Не удалось получить фото. Попробуй позже.') from exc
    if not photos:
        raise ExternalServiceError('В этот раз подходящего фото не нашлось. Запусти /geoguess ещё раз.')
    return random.choice(photos)
