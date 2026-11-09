"""Identity-bound supplier media only; no downloads, geocoding or invented routes."""

import math, re
from urllib.parse import urlsplit


def photo_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        u = urlsplit(value)
        if (
            u.scheme != "https"
            or u.hostname != "i.travelapi.com"
            or u.username
            or u.password
            or u.port not in (None, 443)
            or u.query
            or u.fragment
        ):
            return None
        if not re.fullmatch(
            r"/lodging/[A-Za-z0-9_./-]+\.(?:jpg|jpeg|png|webp)", u.path, re.I
        ) or ".." in u.path.split("/"):
            return None
        return value
    except ValueError:
        return None


def point(lat, lon):
    def number(value):
        if type(value) in (int, float):
            return value
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{1,12})?", value):
            return float(value)

    lat, lon = number(lat), number(lon)
    if (
        lat is None
        or lon is None
        or not math.isfinite(lat)
        or not math.isfinite(lon)
        or not -90 <= lat <= 90
        or not -180 <= lon <= 180
    ):
        return None
    return {"latitude": lat, "longitude": lon}


def extract(detail, provider):
    # These exact TourMind fields are present in the retained real detail
    # captures. Other provider shapes remain unknown until verified.
    if provider != "TOURMIND_DETAIL":
        return {"photos": [], "location": None}
    values = [detail.get("hotel_image")]
    if isinstance(detail.get("hotel_images"), list):
        values += detail["hotel_images"][:50]
    photos = list(dict.fromkeys(u for v in values if (u := photo_url(v))))[:6]
    return {"photos": photos, "location": point(detail.get("latitude"), detail.get("longitude"))}
