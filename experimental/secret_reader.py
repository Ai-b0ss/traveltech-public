"""Credential decoding only; no logging, copying, or key material serialization."""

from pathlib import Path
import unicodedata


def read_key(path=None):
    raw = Path(path or Path.home() / ".traveltech-secrets/reformboss.key").read_bytes()
    if not 1 <= len(raw) <= 32768:
        raise ValueError("KEY_FILE_SIZE_INVALID")
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        value = raw.decode("utf-32")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        value = raw.decode("utf-16")
    elif len(raw) % 2 == 0 and all(x == 0 for x in raw[1::2]) and all(x != 0 for x in raw[::2]):
        value = raw.decode("utf-16-le")
    elif len(raw) % 2 == 0 and all(x == 0 for x in raw[::2]) and all(x != 0 for x in raw[1::2]):
        value = raw.decode("utf-16-be")
    else:
        value = raw.decode("utf-8-sig")
    value = value.strip("\ufeff\x00\r\n\t ")
    while value and (value[0].isspace() or unicodedata.category(value[0]) in {"Cc", "Cf"}):
        value = value[1:]
    while value and (value[-1].isspace() or unicodedata.category(value[-1]) in {"Cc", "Cf"}):
        value = value[:-1]
    if not value:
        raise ValueError("KEY_FILE_EMPTY_AFTER_NORMALIZATION")
    if len(value) > 8192 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("KEY_FILE_FORMAT_INVALID")
    return value
