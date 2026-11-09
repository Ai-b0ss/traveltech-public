"""Strict public-geocoder fallback for unknown Russian city names.

Only a compact destination phrase in a travel request is looked up. The public
Open-Meteo Geocoding API returns GeoNames city identity and ISO country.
We require an exact Russian city name, city feature code, a uniquely dominant
candidate, and a separate English-name lookup for the canonical Request.
No model guesses, synthetic locations, or credentials.
"""
from functools import lru_cache
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://geocoding-api.open-meteo.com/v1/"
CITY_NAME = re.compile(
    r"(?<!\w)(?:в|во)\s+(?:городе?\s+)?([а-яё][а-яё-]{2,34})(?!\w)", re.I
)
NON_PLACES = {
    "одиночку", "солo", "соло", "отпуск", "поездку", "гостиницу",
    "отель", "город", "будни", "выходные", "поход", "путешествие",
    "сторону", "составе", "числе", "рамках", "пределах",
}
TRIP_CONTEXT = re.compile(
    r"\b(?:хочу|поездк\w*|поехать|ехать|еду|поеду|лечу|полететь|"
    r"отправ\w*|путешеств\w*|отдых\w*|отел\w*|"
    r"гостиниц\w*|номер\w*|бюджет)\b", re.I
)


def candidate_from_user_text(text):
    if not isinstance(text, str) or not TRIP_CONTEXT.search(text):
        return None
    cleaned = re.sub(r'«[^»]*»|"[^"]*"', " ", text)
    found = {
        m.group(1).casefold()
        for m in CITY_NAME.finditer(cleaned)
        if m.group(1).casefold() not in NON_PLACES
    }
    return next(iter(found)) if len(found) == 1 else None


def _json(endpoint, params):
    url = API + endpoint + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "TravelTechMVP/0.1 (read-only geocoding)"})
    with urlopen(req, timeout=4) as response:
        if response.status != 200:
            raise ValueError("GEOCODER_HTTP_FAILED")
        size = response.read(150001)
        if len(size) > 150000:
            raise ValueError("GEOCODER_TOO_LARGE")
    return json.loads(size)


def _lookup_openmeteo(candidate):
    return _json("search", {
        "name": candidate, "count": 10, "language": "ru", "format": "json",
    })


def _lookup_english_name(geonames_id):
    return _json("get", {"id": geonames_id, "language": "en"})


@lru_cache(maxsize=256)
def resolve(candidate):
    if not isinstance(candidate, str) or not re.fullmatch(r"[а-яё-]{3,35}", candidate):
        return None
    try:
        matches = _lookup_openmeteo(candidate).get("results", [])
        exact = [
            item for item in matches
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].casefold() == candidate
            and item.get("feature_code", "").startswith("PPL")
            and isinstance(item.get("country_code"), str)
            and re.fullmatch("[A-Z]{2}", item["country_code"])
            and isinstance(item.get("id"), int)
        ]
        if not exact:
            return None
        exact.sort(key=lambda item: int(item.get("population") or 0), reverse=True)
        chosen = exact[0]
        # Equivalent exact city names in different countries require
        # a strong population advantage, not a first-result guess.
        others = [
            int(item.get("population") or 0)
            for item in exact[1:]
            if item["country_code"] != chosen["country_code"]
        ]
        if others and not (
            chosen.get("feature_code") == "PPLC"
            and int(chosen.get("population") or 0) >= 100000
            and int(chosen.get("population") or 0) >= 10 * max(others)
        ):
            return None
        translated = _lookup_english_name(chosen["id"])
        name = translated.get("name")
        if (
            translated.get("id") != chosen["id"]
            or translated.get("country_code") != chosen["country_code"]
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Z][A-Za-z]+(?:[ -][A-Z][A-Za-z]+){0,3}", name)
        ):
            return None
        return name, chosen["country_code"], "live_dynamic"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
