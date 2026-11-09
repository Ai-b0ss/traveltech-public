"""Safe, bounded HTTP transport for airport transfer public GET requests.

Strict constraints:
1. Max 6 public GET calls budget enforced across the process.
2. Zero retries.
3. Bounded payload size (<= 2MB).
4. No credentials, tokens, cookies or personal data sent.
5. Strict JSON parsing (duplicate keys and non-finite floats rejected).
6. Honest error classification (NETWORK_BLOCKED, TIMEOUT, UPSTREAM_ERROR, etc.).
"""

from __future__ import annotations

from http.client import HTTPException
import json
import socket
import threading
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import TransferError

MAX_CALL_BUDGET = 6
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
DEFAULT_TIMEOUT_SECONDS = 10.0

_budget_lock = threading.Lock()
_live_call_count = 0


def get_live_call_count() -> int:
    """Return the total number of live GET calls executed in this process."""
    with _budget_lock:
        return _live_call_count


def reset_live_call_count_for_testing() -> None:
    """Reset the live call counter (strictly for test isolation)."""
    global _live_call_count
    with _budget_lock:
        _live_call_count = 0


def strict_json_loads(raw: bytes | str) -> Any:
    """Parse JSON strictly: reject duplicate keys and non-finite values (NaN/Infinity)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    def _no_duplicates(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise TransferError(
                    "MALFORMED_RESPONSE", details=f"Duplicate key in JSON: {k}"
                )
            out[k] = v
        return out

    def _no_constants(val):
        raise TransferError(
            "MALFORMED_RESPONSE", details=f"Non-finite JSON number rejected: {val}"
        )

    try:
        return json.loads(
            raw,
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constants,
        )
    except json.JSONDecodeError as exc:
        raise TransferError("MALFORMED_RESPONSE", details=f"Invalid JSON: {exc}") from exc


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse automatic redirects to prevent unexpected outbound hops."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not follow redirects
        return None


# Type alias for custom transport callable in tests:
# (url, params, headers, timeout) -> tuple[int, bytes]
TransportFunc = Callable[[str, dict[str, str] | None, dict[str, str] | None, float], tuple[int, bytes]]


def default_live_transport(
    url: str,
    params: dict[str, str] | None,
    headers: dict[str, str] | None,
    timeout: float,
) -> tuple[int, bytes]:
    """Execute a single bounded public GET request with no retries."""
    global _live_call_count
    with _budget_lock:
        if _live_call_count >= MAX_CALL_BUDGET:
            raise TransferError(
                "CALL_BUDGET_EXCEEDED",
                details=f"Live public GET call budget ({MAX_CALL_BUDGET}) reached; no more calls permitted",
            )
        _live_call_count += 1

    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"

    req_headers = {
        "User-Agent": "TravelTechMVP/1.0 (PublicTransitResearch; ReadOnly)",
        "Accept": "application/json",
    }
    if headers:
        for k, v in headers.items():
            # Security guard: never allow Authorization or Cookie headers in public transfers
            if k.lower() in ("authorization", "cookie", "proxy-authorization", "x-api-key"):
                raise TransferError(
                    "SECURITY_VIOLATION",
                    details=f"Header {k} is strictly forbidden on public read-only transport",
                )
            req_headers[k] = v

    request = Request(full_url, headers=req_headers, method="GET")
    opener = build_opener(_NoRedirectHandler())

    try:
        try:
            with opener.open(request, timeout=timeout) as response:
                status = response.status
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as err:
            with err:
                status = err.code
                body = err.read(MAX_RESPONSE_BYTES + 1)
    except (socket.timeout, TimeoutError):
        raise TransferError("TIMEOUT", details=f"Connection timed out after {timeout}s") from None
    except (URLError, OSError, HTTPException) as err:
        raise TransferError("NETWORK_ERROR", details=f"Network error contacting {url}: {err}") from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise TransferError(
            "RESPONSE_TOO_LARGE",
            http_status=status,
            details=f"Response exceeded maximum allowed size of {MAX_RESPONSE_BYTES} bytes",
        )

    return status, body


class SafeHTTPClient:
    """Safe HTTP client with support for custom mock/offline transport."""

    def __init__(self, transport: TransportFunc | None = None):
        self._transport = transport or default_live_transport

    def get_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> tuple[int, Any]:
        """Perform GET and parse JSON response, classifying HTTP errors."""
        try:
            status, raw_bytes = self._transport(url, params, headers, timeout)
        except (socket.timeout, TimeoutError):
            raise TransferError("TIMEOUT", details=f"Connection timed out after {timeout}s") from None

        if status == 300:
            # Disambiguation: return status and parsed body so caller can extract choices
            parsed = strict_json_loads(raw_bytes)
            return status, parsed

        if status in (401, 403):
            raise TransferError(
                "NETWORK_BLOCKED",
                http_status=status,
                details=f"Public access blocked by provider (HTTP {status})",
            )
        elif status == 429:
            raise TransferError(
                "RATE_LIMITED",
                http_status=status,
                details="Rate limit reached on provider API",
            )
        elif 500 <= status <= 599:
            raise TransferError(
                "UPSTREAM_ERROR",
                http_status=status,
                details=f"Upstream provider error (HTTP {status})",
            )
        elif status != 200:
            raise TransferError(
                "HTTP_ERROR",
                http_status=status,
                details=f"Unexpected HTTP status {status}",
            )

        parsed = strict_json_loads(raw_bytes)
        return status, parsed
