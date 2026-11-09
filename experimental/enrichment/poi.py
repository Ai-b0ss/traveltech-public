"""Fact-only nearby POI from Wikidata and Wikipedia GeoSearch.

Remote calls are off unless ``allow_remote=True``. The only live smoke coordinate
is the public Eiffel Tower benchmark 48.8584, 2.2945. This is not a device location.

Wikidata GeoSearch titles are QIDs. Those facts are CC0. Wikipedia GeoSearch
returns article titles, coordinates and provider distances. Titles are stored as
untrusted display text with CC BY-SA attribution. Article prose is not requested
and must not be copied. A nearby encyclopaedia row is not a bookable attraction:
``ticket_price`` and ``opening_hours`` stay None, and ``attraction_verified`` is False.

Wikivoyage (and any MediaWiki body) that contains an ``error`` object is an
application error even when HTTP status is 200. No URL is invented for a POI.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .errors import EnrichmentError
from .http_get import http_get, status_code, verified_tls_context

POI_LIVE_ENABLED = False
EIFFEL_SMOKE_LAT = Decimal("48.8584")
EIFFEL_SMOKE_LON = Decimal("2.2945")
MAX_BODY_BYTES = 524288
DEFAULT_TIMEOUT_SECONDS = 12.0
FRESHNESS_TTL = timedelta(hours=24)
MIN_RADIUS_M = 1
MAX_RADIUS_M = 10000
MIN_LIMIT = 1
MAX_LIMIT = 20
QID_RE = re.compile(r"^Q[1-9][0-9]*$")
TAG_RE = re.compile(r"<[^>]*>")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

HOSTS = {
    "wikidata": "www.wikidata.org",
    "wikipedia": "en.wikipedia.org",
    "wikivoyage": "en.wikivoyage.org",
}
RIGHTS = {
    "wikidata": "CC0",
    "wikipedia": "CC BY-SA",
    "wikivoyage": "CC BY-SA",
}
ATTRIBUTION = {
    "wikidata": "Wikidata facts (CC0): QID, coordinates and distance. No ticket or hours.",
    "wikipedia": "English Wikipedia page titles (CC BY-SA). No article prose stored.",
    "wikivoyage": "Wikivoyage titles (CC BY-SA). An HTTP 200 error object is still an error.",
}
_INJECTION = re.compile(
    r"(ignore\s+(all\s+)?previous(\s+instructions)?|system\s+prompt|<\s*/?\s*script|javascript:|onerror\s*=|\{\{)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PoiFact:
    title: str
    latitude: Decimal
    longitude: Decimal
    distance_m: Decimal
    distance_origin: str
    qid: str | None
    source: str
    source_url: str
    rights: str
    attribution: str
    observed_at: datetime
    untrusted_display_text: bool = True
    ticket_price: None = None
    opening_hours: None = None
    attraction_verified: bool = False
    bookable: bool = False

    def __post_init__(self) -> None:
        if self.ticket_price is not None or self.opening_hours is not None:
            raise EnrichmentError("UNVERIFIED_COMMERCIAL_FACT")
        if self.attraction_verified or self.bookable:
            raise EnrichmentError("UNVERIFIED_ATTRACTION")
        if self.untrusted_display_text is not True:
            raise EnrichmentError("UNTRUSTED_TEXT")
        if self.source not in HOSTS:
            raise EnrichmentError("POI_SOURCE")
        if self.rights != RIGHTS[self.source]:
            raise EnrichmentError("RIGHTS")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise EnrichmentError("NAIVE_TIMESTAMP")
        _coordinate(self.latitude, self.longitude)
        if not isinstance(self.distance_m, Decimal) or not self.distance_m.is_finite() or self.distance_m < 0:
            raise EnrichmentError("DISTANCE")
        if self.qid is not None and QID_RE.fullmatch(self.qid) is None:
            raise EnrichmentError("QID")


def _aware_utc(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise EnrichmentError("NAIVE_TIMESTAMP")
    return current.astimezone(timezone.utc)


def _coordinate(latitude, longitude) -> tuple[Decimal, Decimal]:
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        raise EnrichmentError("COORDINATE")
    if not isinstance(latitude, Decimal) or not isinstance(longitude, Decimal):
        raise EnrichmentError("COORDINATE")
    if not latitude.is_finite() or not longitude.is_finite():
        raise EnrichmentError("NONFINITE")
    if latitude < Decimal("-90") or latitude > Decimal("90"):
        raise EnrichmentError("COORDINATE")
    if longitude < Decimal("-180") or longitude > Decimal("180"):
        raise EnrichmentError("COORDINATE")
    return latitude, longitude


def _radius(radius_m: int) -> int:
    if isinstance(radius_m, bool) or not isinstance(radius_m, int):
        raise EnrichmentError("RADIUS")
    if radius_m < MIN_RADIUS_M or radius_m > MAX_RADIUS_M:
        raise EnrichmentError("RADIUS")
    return radius_m


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise EnrichmentError("LIMIT")
    if limit < MIN_LIMIT or limit > MAX_LIMIT:
        raise EnrichmentError("LIMIT")
    return limit


def sanitize_untrusted_text(value) -> str:
    """Display text only. Tags and control characters are removed. Nothing is executed.

    Instruction-like words stay in the string. Callers must treat the result as
    data. Deleting those words would also hide a real title.
    """
    if not isinstance(value, str):
        raise EnrichmentError("MALFORMED")
    cleaned = _INJECTION.sub(" ", value)
    cleaned = TAG_RE.sub("", cleaned)
    cleaned = cleaned.replace("<", " ").replace(">", " ")
    cleaned = CONTROL_RE.sub("", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise EnrichmentError("MALFORMED")
    return cleaned[:240]


def build_geosearch_url(source: str, latitude: Decimal, longitude: Decimal, radius_m: int, limit: int) -> str:
    if source not in HOSTS:
        raise EnrichmentError("POI_SOURCE")
    latitude, longitude = _coordinate(latitude, longitude)
    radius_m = _radius(radius_m)
    limit = _limit(limit)
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "geosearch",
            "gscoord": f"{latitude}|{longitude}",
            "gsradius": str(radius_m),
            "gslimit": str(limit),
        }
    )
    return f"https://{HOSTS[source]}/w/api.php?{query}"


def _strict_json(raw: bytes) -> object:
    if len(raw) > MAX_BODY_BYTES:
        raise EnrichmentError("OVERSIZE")

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise EnrichmentError("MALFORMED")
            out[key] = value
        return out

    def reject_constant(_value):
        raise EnrichmentError("NONFINITE")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=reject_constant,
        )
    except EnrichmentError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise EnrichmentError("MALFORMED") from None


def _decimal_field(value, code: str) -> Decimal:
    if isinstance(value, str):
        try:
            value = Decimal(value)
        except InvalidOperation:
            raise EnrichmentError(code) from None
    if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
        raise EnrichmentError(code)
    return value


def _qid_from_title(source: str, title: str, pageprops) -> str | None:
    if isinstance(pageprops, dict) and isinstance(pageprops.get("wikibase_item"), str):
        qid = pageprops["wikibase_item"]
        if QID_RE.fullmatch(qid) is None:
            raise EnrichmentError("QID")
        return qid
    if source == "wikidata" and QID_RE.fullmatch(title):
        return title
    return None


def _row_to_fact(source: str, row: dict, *, source_url: str, observed_at: datetime, origin: tuple[Decimal, Decimal]) -> PoiFact:
    if not isinstance(row, dict):
        raise EnrichmentError("MALFORMED")
    title = sanitize_untrusted_text(row.get("title"))
    latitude = _decimal_field(row.get("lat"), "COORDINATE")
    longitude = _decimal_field(row.get("lon"), "COORDINATE")
    _coordinate(latitude, longitude)
    if "dist" in row and row["dist"] is not None:
        distance = _decimal_field(row["dist"], "DISTANCE")
        if distance < 0:
            raise EnrichmentError("DISTANCE")
        origin_kind = "provider"
    else:
        distance = _haversine_m(origin[0], origin[1], latitude, longitude)
        origin_kind = "haversine_wgs84"
    pageprops = row.get("pageprops")
    qid = _qid_from_title(source, title, pageprops)
    if source == "wikidata" and qid is None:
        raise EnrichmentError("QID")
    return PoiFact(
        title=title,
        latitude=latitude,
        longitude=longitude,
        distance_m=distance,
        distance_origin=origin_kind,
        qid=qid,
        source=source,
        source_url=source_url,
        rights=RIGHTS[source],
        attribution=ATTRIBUTION[source],
        observed_at=observed_at,
    )


def _haversine_m(lat1: Decimal, lon1: Decimal, lat2: Decimal, lon2: Decimal) -> Decimal:
    """Fallback only when the provider omitted ``dist``. Result is quantized to 0.1 m."""
    import math

    radius = 6371000.0
    p1, l1, p2, l2 = map(float, (lat1, lon1, lat2, lon2))
    phi1, phi2 = math.radians(p1), math.radians(p2)
    dphi = math.radians(p2 - p1)
    dlambda = math.radians(l2 - l1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    metres = 2 * radius * math.asin(min(1.0, math.sqrt(a)))
    if metres != metres or metres == float("inf"):
        raise EnrichmentError("NONFINITE")
    return Decimal(str(round(metres, 1)))


def _geosearch_rows(payload: dict) -> list:
    if "error" in payload:
        detail = payload.get("error")
        info = detail.get("code") if isinstance(detail, dict) else "error"
        if not isinstance(info, str) or not info:
            info = "error"
        raise EnrichmentError("APPLICATION_ERROR", info)
    query = payload.get("query")
    if not isinstance(query, dict):
        raise EnrichmentError("MALFORMED")
    rows = query.get("geosearch")
    if isinstance(rows, list):
        return rows
    pages = query.get("pages")
    if isinstance(pages, list):
        normalized = []
        for page in pages:
            if not isinstance(page, dict):
                raise EnrichmentError("MALFORMED")
            coords = page.get("coordinates")
            if not isinstance(coords, list) or not coords or not isinstance(coords[0], dict):
                raise EnrichmentError("MALFORMED")
            primary = coords[0]
            normalized.append(
                {
                    "title": page.get("title"),
                    "lat": primary.get("lat"),
                    "lon": primary.get("lon"),
                    "dist": primary.get("dist"),
                    "pageprops": page.get("pageprops"),
                }
            )
        return normalized
    if isinstance(pages, dict):
        raise EnrichmentError("MALFORMED")
    raise EnrichmentError("MALFORMED")


def parse_geosearch(
    raw: bytes,
    *,
    source: str,
    latitude: Decimal,
    longitude: Decimal,
    limit: int,
    source_url: str,
    observed_at: datetime,
) -> tuple[PoiFact, ...]:
    if source not in HOSTS:
        raise EnrichmentError("POI_SOURCE")
    origin = _coordinate(latitude, longitude)
    limit = _limit(limit)
    observed = _aware_utc(observed_at)
    payload = _strict_json(raw)
    if not isinstance(payload, dict):
        raise EnrichmentError("MALFORMED")
    host = urllib.parse.urlparse(source_url).hostname
    if urllib.parse.urlparse(source_url).scheme != "https" or host != HOSTS[source]:
        raise EnrichmentError("SOURCE_URL")
    rows = _geosearch_rows(payload)
    if not rows:
        raise EnrichmentError("NO_POI")
    if len(rows) > limit:
        rows = rows[:limit]
    facts = tuple(
        _row_to_fact(source, row, source_url=source_url, observed_at=observed, origin=origin) for row in rows
    )
    return facts


def nearby_pois(
    latitude,
    longitude,
    *,
    radius_m: int = 850,
    limit: int = 12,
    source: str = "wikidata",
    transport=None,
    allow_remote: bool | None = None,
    observed_at: datetime | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[PoiFact, ...]:
    latitude, longitude = _coordinate(latitude, longitude)
    url = build_geosearch_url(source, latitude, longitude, radius_m, limit)
    current = _aware_utc(observed_at)
    enabled = POI_LIVE_ENABLED if allow_remote is None else allow_remote
    if transport is None and not enabled:
        raise EnrichmentError("LIVE_DISABLED")
    if transport is None:
        def transport(target: str):
            return http_get(target, timeout=timeout, max_bytes=MAX_BODY_BYTES, ssl_context=verified_tls_context())
    try:
        response = transport(url)
    except EnrichmentError:
        raise
    except TimeoutError:
        raise EnrichmentError("TIMEOUT") from None
    if getattr(response, "status", 200) != 200:
        raise EnrichmentError(status_code(response.status))
    body = response.body
    if len(body) > MAX_BODY_BYTES:
        raise EnrichmentError("OVERSIZE")
    facts = parse_geosearch(
        body,
        source=source,
        latitude=latitude,
        longitude=longitude,
        limit=limit,
        source_url=getattr(response, "url", None) or url,
        observed_at=current,
    )
    age = _aware_utc(None) - facts[0].observed_at
    if age < timedelta(0) or age > FRESHNESS_TTL:
        raise EnrichmentError("STALE")
    return facts


def live_eiffel_smoke(source: str = "wikidata", *, transport=None, allow_remote: bool = True, **kwargs):
    """Read-only smoke locked to the published Eiffel Tower coordinate."""
    if kwargs.get("latitude") not in (None, EIFFEL_SMOKE_LAT) or kwargs.get("longitude") not in (None, EIFFEL_SMOKE_LON):
        raise EnrichmentError("SMOKE_COORDINATE_LOCKED")
    return nearby_pois(
        EIFFEL_SMOKE_LAT,
        EIFFEL_SMOKE_LON,
        radius_m=kwargs.get("radius_m", 850),
        limit=kwargs.get("limit", 12),
        source=source,
        transport=transport,
        allow_remote=allow_remote,
        observed_at=kwargs.get("observed_at"),
    )
