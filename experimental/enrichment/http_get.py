"""One bounded anonymous HTTPS GET. Redirects and retries are refused."""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request

from .errors import EnrichmentError

USER_AGENT = "TravelTech-enrichment/20261025 (educational read-only; local hackathon client)"


class BoundedResponse:
    def __init__(self, status: int, body: bytes, url: str):
        self.status = status
        self.body = body
        self.url = url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EnrichmentError("REDIRECT_REFUSED")


def status_code(code: int) -> str:
    if code == 400:
        return "HTTP_400"
    if code == 401:
        return "HTTP_401"
    if code == 404:
        return "HTTP_404"
    if code == 429:
        return "HTTP_429"
    if 500 <= code <= 599:
        return "HTTP_5XX"
    return "HTTP_STATUS"


def verified_tls_context() -> ssl.SSLContext:
    """Same rule as experimental/trusted_tls.py: drop expired OS anchors.

    The default Windows store on this machine fails Wikimedia with an expired
    anchor. Building a context from currently valid anchors keeps verification on.
    """
    try:
        from experimental.trusted_tls import verified_context
    except ImportError:
        from trusted_tls import verified_context
    return verified_context()


def http_get(url: str, *, timeout: float, max_bytes: int, ssl_context: ssl.SSLContext | None = None) -> BoundedResponse:
    if timeout <= 0 or max_bytes < 1:
        raise EnrichmentError("REQUEST_LIMIT")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    handlers: list = [_NoRedirect()]
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read(max_bytes + 1)
            final_url = response.geturl()
    except EnrichmentError:
        raise
    except urllib.error.HTTPError as exc:
        raise EnrichmentError(status_code(exc.code)) from None
    except (TimeoutError, socket.timeout):
        raise EnrichmentError("TIMEOUT") from None
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).lower()
        if "timed out" in reason or "timeout" in reason:
            raise EnrichmentError("TIMEOUT") from None
        raise EnrichmentError("TRANSPORT") from None
    if status != 200:
        raise EnrichmentError(status_code(status))
    if len(body) > max_bytes:
        raise EnrichmentError("OVERSIZE")
    if final_url != url:
        raise EnrichmentError("REDIRECT_REFUSED")
    return BoundedResponse(status, body, final_url)
