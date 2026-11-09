"""Windows user-scoped DPAPI encryption for TravelTech's durable model replies.

Never persist unencrypted model text. If DPAPI is unavailable, fail closed:
the original request remains reserved and cannot be automatically retried.
"""

import ctypes
import hashlib
import os
from ctypes import wintypes

from ai_router import AdapterError


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(raw):
    buffer = ctypes.create_string_buffer(raw)
    return DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _call(data, entropy, *, decrypt=False):
    if os.name != "nt":
        raise AdapterError("RESPONSE_CACHE_OS_PROTECTION_UNAVAILABLE")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER(DATA_BLOB)
        if decrypt:
            function = crypt32.CryptUnprotectData
            function.argtypes = [
                pointer,
                ctypes.POINTER(wintypes.LPWSTR),
                pointer,
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                pointer,
            ]
        else:
            function = crypt32.CryptProtectData
            function.argtypes = [
                pointer,
                wintypes.LPCWSTR,
                pointer,
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                pointer,
            ]
        function.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        source, source_memory = _input_blob(data)
        extra, extra_memory = _input_blob(entropy)
        result = DATA_BLOB()
        # CRYPTPROTECT_UI_FORBIDDEN: zero interactive secret/UI access.
        if decrypt:
            ok = function(
                ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(result)
            )
        else:
            ok = function(
                ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(result)
            )
        if not ok:
            raise AdapterError(
                "RESPONSE_CACHE_DECRYPT_FAILED" if decrypt else "RESPONSE_CACHE_ENCRYPT_FAILED"
            )
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))
    except AdapterError:
        raise
    except (OSError, AttributeError, TypeError, ValueError):
        raise AdapterError(
            "RESPONSE_CACHE_DECRYPT_FAILED" if decrypt else "RESPONSE_CACHE_ENCRYPT_UNAVAILABLE"
        ) from None


def _binding(identifier, provider, model, digest):
    return hashlib.sha256(
        (
            "traveltech-response-v1:" + identifier + ":" + provider + ":" + model + ":" + digest
        ).encode("utf8")
    ).digest()


def seal_reply(data, identifier, provider, model, digest):
    return _call(data, _binding(identifier, provider, model, digest))


def open_reply(sealed, identifier, provider, model, digest):
    return _call(sealed, _binding(identifier, provider, model, digest), decrypt=True)
