"""Known archive cities and strict country-qualified live discovery (fail closed)."""

import json, os, re
from pathlib import Path
from travel_core import InputError
from contextlib import contextmanager
from contextvars import ContextVar


_geocoding_allowed = ContextVar("geocoding_allowed", default=True)


@contextmanager
def geocoding_policy(allow_network):
    """A local request cannot enable geocoding, including in nested validation."""
    token = _geocoding_allowed.set(_geocoding_allowed.get() and allow_network)
    try:
        yield
    finally:
        _geocoding_allowed.reset(token)

ALIASES = {
    "сочи": ("Сочи", "RU", "ru"),
    "sochi": ("Сочи", "RU", "ru"),
    "dubai": ("Dubai", "AE", "dubai"),
    "дубай": ("Dubai", "AE", "dubai"),
    "istanbul": ("Istanbul", "TR", "istanbul"),
    "frankfurt am main": ("Frankfurt am Main", "DE", "live_dynamic"),
    "стамбул": ("Istanbul", "TR", "istanbul"),
}
TEXT_NAMES = {**ALIASES, "дубае": ALIASES["дубай"], "стамбуле": ALIASES["стамбул"]}
COUNTRIES = json.loads(
    (Path(__file__).resolve().parent / "country_names_iso2.json").read_text(encoding="utf-8")
)
# Explicit country abbreviation is mandatory for arbitrary cities.
# Text in parentheses or reviews is NOT accepted by the matcher.
DYNAMIC_LOCATION_RE = re.compile(
    r"(?<!\w)(?:в|во|in)\s+(?:городе?\s+|city\s+)?([A-Z][A-Za-z]+(?:[ -][A-Z][A-Za-z]+){0,3})\s*,\s*(?:страна\s+|country\s+)?([A-Z]{2})(?=\s|[,.;]|$)"
)


def dynamic_explicit_location(text):
    if not isinstance(text, str):
        return None
    matches = list(DYNAMIC_LOCATION_RE.finditer(text))
    if not matches:
        return None
    records = {(x.group(1), x.group(2)) for x in matches if x.group(2) in COUNTRIES}
    return (
        (next(iter(records))[0], next(iter(records))[1], "live_dynamic")
        if len(records) == 1
        else None
    )


from ru_city_registry import ALIASES as RUSSIAN_ALIASES, matches as russian_city_matches

COUNTRY_WORDS = {
    "сша": "US",
    "франция": "FR",
    "франции": "FR",
    "германия": "DE",
    "германии": "DE",
    "италия": "IT",
    "италии": "IT",
    "япония": "JP",
    "японии": "JP",
    "россия": "RU",
    "россии": "RU",
}


def canonical_city(value, country=None):
    if not isinstance(value, str):
        raise InputError("UNKNOWN_CITY_NO_GUESS")
    known = ALIASES.get(value.casefold())
    if known:
        if country is not None and country != known[1]:
            raise InputError("CITY_COUNTRY_MISMATCH")
        return known
    curated = RUSSIAN_ALIASES.get(value.casefold())
    if curated:
        city, cc = curated
        if country is not None and country != cc:
            raise InputError("CITY_COUNTRY_MISMATCH")
        return ALIASES.get(city.casefold(), (city, cc, "live_dynamic"))
    if country not in COUNTRIES or not re.fullmatch(
        r"[A-Z][A-Za-z]+(?:[ -][A-Z][A-Za-z]+){0,3}", value
    ):
        raise InputError("UNKNOWN_CITY_REQUIRES_EXPLICIT_COUNTRY")
    return value, country, "live_dynamic"


def location_in_text(text):
    """Resolve a destination only if its identity and country are unambiguous.

    Try known aliases first, then the strict geocoder fallback for explicit
    unknown city names in deterministic MVP mode. Never choose the first
    possible match on the strength of a fuzzy geocoder response.
    """
    if not isinstance(text, str):
        return None
    # A location explicitly in a quoted supplier comment is not a new user choice.
    cleaned = re.sub(r'«[^»]*»|"[^"]*"', " ", text)
    legacy = [
        value
        for name, value in TEXT_NAMES.items()
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", cleaned, re.I)
    ]
    dynamic = dynamic_explicit_location(cleaned)
    russian = russian_city_matches(cleaned)
    choices = {(x[0].casefold(), x[1]) for x in legacy}
    choices.update((canonical_city(c, cc)[0].casefold(), cc) for c, cc in russian)
    if dynamic:
        choices.add((dynamic[0].casefold(), dynamic[1]))
    if (not choices and _geocoding_allowed.get()
            and os.environ.get("TRAVELTECH_MVP_DETERMINISTIC") == "1"):
        # No new fixed-city dictionary: geocoder is only used for unknown,
        # explicit Russian destinations, and fails closed on ambiguity.
        from geo_city_resolver import candidate_from_user_text, resolve
        candidate = candidate_from_user_text(cleaned)
        # Known ambiguous shorthand and explicit typo suggestions require a
        # user's choice even if a geocoder happens to return only one result.
        ambiguous = candidate in {"франкфурт", "франкфурте", "франкфурта"}
        if (candidate and candidate not in RUSSIAN_ALIASES
                and not ambiguous and city_typo_candidate(cleaned) is None):
            verified = resolve(candidate)
            if verified:
                # A named country after the city must agree with the geocoder.
                explicit = re.findall(
                    r"\b" + re.escape(candidate) +
                    r"\s*,\s*(?:страна\s+)?([A-Za-zА-Яа-яёЁ]{2,35})",
                    cleaned, re.I,
                )
                for token in explicit:
                    code = (
                        token.upper()
                        if token.upper() in COUNTRIES
                        else COUNTRY_WORDS.get(token.casefold())
                    )
                    if code and code != verified[1]:
                        return None
                return canonical_city(verified[0], verified[1])
    if len(choices) != 1:
        return None
    city, country = next(iter(choices))
    # Explicit nearby code/country words override inference; conflicting codes fail.
    suffixes = re.findall(r"(?:,\s*|\bстрана\s+)([A-Z]{2})(?=\W|$)", cleaned)
    literal_countries = [
        COUNTRY_WORDS[x.lower()]
        for x in re.findall(
            r"\b(?:сша|франци[яи]|германи[яи]|итали[яи]|япони[яи]|росси[яи])\b", cleaned, re.I
        )
    ]
    if any(x != country for x in suffixes + literal_countries):
        return None
    if dynamic:
        return canonical_city(dynamic[0], dynamic[1])
    if legacy:
        return legacy[0] if all(x == legacy[0] for x in legacy) else None
    src = next((x for x in russian if x[0].casefold() == city and x[1] == country), None)
    return canonical_city(src[0], src[1]) if src else None


def city_typo_candidate(text):
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r'«[^»]*»|"[^"]*"', " ", text)
    tokens = re.findall(r"\b[А-Яа-яA-Za-z]{4,15}\b", cleaned)
    for t in tokens:
        tl = t.lower()
        if tl in ("стабмул", "стабмуле", "стамбулом", "стамбула", "стамбулу"):
            return ("Istanbul", "TR", "Стамбул", t)
        if tl in ("сочии", "сочах"):
            return ("Сочи", "RU", "Сочи", t)
        if tl in ("дубаи", "дубаях", "дубаем"):
            return ("Dubai", "AE", "Дубай", t)
    return None
