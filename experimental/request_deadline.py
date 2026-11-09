"""One monotonic budget per Skills run; nested optional work cannot extend it.

Socket timeouts are capped at remaining time. Body reads use read1 and reset
the timeout before each receive, so a slowly streaming body cannot reset the
whole request budget. DNS and OS scheduling are not a hard realtime guarantee.
"""

import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from provider_transport import Failure

DEFAULT_SECONDS = 240.0
OPTIONAL_SECONDS = 8.0
CRITICAL_RESERVE_SECONDS = 60.0
_deadline = ContextVar("traveltech_request_deadline", default=None)


def current():
    return _deadline.get()


def remaining():
    end = current()
    return None if end is None else max(0.0, end - time.monotonic())


def check():
    left = remaining()
    if left is not None and left <= 0:
        raise Failure("REQUEST_DEADLINE_EXCEEDED")


def timeout(maximum):
    check()
    left = remaining()
    return maximum if left is None else min(maximum, left)


@contextmanager
def budget(seconds=DEFAULT_SECONDS, *, end=None):
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise ValueError("REQUEST_BUDGET_INVALID")
    parent = current()
    expires = time.monotonic() + seconds if end is None else end
    if parent is not None:
        expires = min(parent, expires)
    token = _deadline.set(expires)
    try:
        check()
        yield
    finally:
        _deadline.reset(token)


def request_budget(seconds=DEFAULT_SECONDS):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            with budget(seconds):
                return fn(*args, **kwargs)

        return run

    return decorate


def sleep(seconds):
    # Used for provider rate slots: never start inference after waiting past
    # the deadline. The reservation can then truthfully record no dispatch.
    check()
    left = remaining()
    time.sleep(seconds if left is None else min(seconds, left))
    check()


def read(response, limit):
    check()
    # Some injected adapters expose a one-shot read(), not a cursor/read1.
    # Preserve that contract rather than concatenating the same body again.
    if current() is None or not callable(getattr(response, "read1", None)):
        return response.read(limit)
    chunks = []
    size = 0
    while size < limit:
        check()
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(timeout(240))
        chunk = getattr(response, "read1", response.read)(min(8192, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)
