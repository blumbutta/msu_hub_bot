"""Random geotagged Commons photos. No media downloads or local cache."""

import asyncio
import math
import os
import time
import random
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

import aiohttp
import pytz
from babel import Locale
from cachetools import TTLCache

from msu_hub_bot.providers.exceptions import ExternalServiceError

# Labels only, never a list of allowed photo locations. ISO countries and territories.
COUNTRIES = {code.lower(): Locale("ru").territories[code] for code in pytz.country_names}
GEOCODER_URL = os.environ.get("GEOGUESS_GEOCODER_URL", "https://nominatim.openstreetmap.org/reverse")
FETCH_TIMEOUT = 8
_geocoder_lock = None
_geocoder_next = 0.0
_geocoder_cache = TTLCache(maxsize=1024, ttl=86400)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value: str, limit: int) -> str:
    parser = PlainText()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())[:limit]


def safe_url(value, hosts):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.netloc in hosts


@dataclass(frozen=True)
class Photo:
    country: str
    city: str
    url: str
    source: str
    author: str
    license: str
    license_url: str


@dataclass(frozen=True)
class Candidate:
    photo: Photo
    latitude: float
    longitude: float


def candidates(data):
    photos = []
    query = data.get("query") if isinstance(data, dict) else None
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, dict):
        return photos
    for page in pages.values():
        try:
            info = page["imageinfo"][0]
            metadata = info["extmetadata"]

            def value(key: str) -> str:
                result = metadata[key]["value"]
                if not isinstance(result, str):
                    raise ValueError("Expected textual photo metadata")
                return result

            width, height = info["width"], info["height"]
            if info["mime"] != "image/jpeg" or type(width) is not int or type(height) is not int or min(width, height) < 600:
                continue
            latitude, longitude = float(value("GPSLatitude")), float(value("GPSLongitude"))
            if not (math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            url = info.get("thumburl", info.get("url"))
            if "thumburl" in info and "thumbwidth" in info and "thumbheight" in info:
                width, height = info["thumbwidth"], info["thumbheight"]
            # Use original dimensions when thumbnail dimensions are unavailable.
            if (
                type(width) is not int
                or type(height) is not int
                or min(width, height) <= 0
                or width + height > 10000
                or max(width, height) > 20 * min(width, height)
            ):
                continue
            license_url = value("LicenseUrl").replace("http://", "https://", 1)
            if not safe_url(url, {"upload.wikimedia.org", "thumb.wikimedia.org"}):
                continue
            if not safe_url(license_url, {"creativecommons.org"}):
                continue
            pageid = int(page["pageid"])
            author = plain(value("Artist"), 90)
            license_name = plain(value("LicenseShortName"), 40)
            if not author or not license_name or pageid <= 0:
                continue
            photos.append(
                Candidate(
                    Photo("", "", url, f"https://commons.wikimedia.org/?curid={pageid}", author, license_name, license_url),
                    latitude,
                    longitude,
                )
            )
        except KeyError, IndexError, TypeError, ValueError:
            continue
    return photos


async def request_json(session, url, params):
    async with session.get(url, params=params, allow_redirects=False) as response:
        if response.status != 200:
            raise ExternalServiceError("Источник сейчас недоступен.")
        data = await response.json()
        if not isinstance(data, dict):
            raise ExternalServiceError("Источник вернул ошибку.")
        return data


class UnknownLocation(ExternalServiceError):
    pass


def location(data):
    address = data.get("address") if isinstance(data, dict) else None
    if not isinstance(address, dict):
        raise UnknownLocation("Не удалось определить страну фотографии.")
    code = address.get("country_code")
    country = address.get("country")
    if not isinstance(code, str) or code.lower() not in COUNTRIES or not isinstance(country, str) or not country.strip():
        raise UnknownLocation("Не удалось определить страну фотографии.")
    city = next((address[key] for key in ("city", "town", "village", "municipality", "county", "state") if address.get(key)), "")
    if not isinstance(city, str):
        raise UnknownLocation("Не удалось определить страну фотографии.")
    return COUNTRIES[code.lower()], plain(city, 100)


async def reverse_location(session, latitude, longitude):
    # Public Nominatim: one request at a time, at most 1/s across all chats.
    # Cache metadata only; images are never downloaded or stored.
    global _geocoder_lock, _geocoder_next
    if _geocoder_lock is None:
        _geocoder_lock = asyncio.Lock()
    key = (latitude, longitude)
    async with _geocoder_lock:
        if key in _geocoder_cache:
            return _geocoder_cache[key]
        await asyncio.sleep(max(0, _geocoder_next - time.monotonic()))
        _geocoder_next = time.monotonic() + 1.1
        data = await request_json(
            session,
            GEOCODER_URL,
            {
                "format": "jsonv2",
                "lat": latitude,
                "lon": longitude,
                "zoom": 10,
                "addressdetails": 1,
                "accept-language": "ru",
            },
        )
        result = location(data)
        _geocoder_cache[key] = result
        return result


async def fetch_photo(recent_countries: tuple[str, ...] = ()) -> Photo:
    # Sample the entire Commons file namespace, then keep usable geotagged photos.
    # No city list, country filter, geographic radius or search-result ranking.
    params = {
        "action": "query",
        "generator": "random",
        "grnnamespace": 6,
        "grnlimit": 30,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime|size",
        "iiextmetadatafilter": "Artist|LicenseShortName|LicenseUrl|GPSLatitude|GPSLongitude",
        "iiurlwidth": 960,
        "format": "json",
        "maxage": 0,
        "smaxage": 0,
    }
    # History is oldest first; a repeated country's latest occurrence determines its priority.
    latest_occurrence = {country: index for index, country in enumerate(recent_countries)}
    fallback: Photo | None = None
    try:
        # Includes the shared geocoder queue and session cleanup. Keep a verified fallback
        # when looking for a new country exhausts the budget, leaving time for Telegram.
        async with asyncio.timeout(FETCH_TIMEOUT):
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                headers={"User-Agent": "MSUHubBot-Geoguess/1.0 (https://github.com/uburuntu/msu_hub_bot)"},
            ) as session:
                data = await request_json(session, "https://commons.wikimedia.org/w/api.php", params)
                photos = candidates(data)
                if not photos:
                    raise ExternalServiceError("Подходящего фото не нашлось.")
                random.shuffle(photos)
                for candidate in photos[:4]:
                    try:
                        country, city = await reverse_location(session, candidate.latitude, candidate.longitude)
                    except UnknownLocation:
                        continue
                    photo = candidate.photo
                    verified = Photo(country, city, photo.url, photo.source, photo.author, photo.license, photo.license_url)
                    if country not in latest_occurrence:
                        return verified
                    if fallback is None or latest_occurrence[country] < latest_occurrence[fallback.country]:
                        fallback = verified
    except ExternalServiceError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError:
        if fallback is None:
            raise
    if fallback is not None:
        return fallback
    raise ExternalServiceError("Нет фото с определённой страной.")


async def random_photo(recent_countries: tuple[str, ...] = ()) -> Photo:
    try:
        # fetch_photo owns its deadline so it can return a verified fallback on timeout.
        # The command additionally limits fetching plus Telegram delivery to 10 seconds.
        return await fetch_photo(recent_countries)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError) as exc:
        raise ExternalServiceError("Не удалось получить фото. Попробуй позже.") from exc
