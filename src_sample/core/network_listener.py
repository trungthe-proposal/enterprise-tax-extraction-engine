"""
network_listener.py
===================

Reference implementation: react to server-side problems within about a second
instead of waiting for a default timeout.

Two complementary signals are observed at the same time:

1. **Network responses** - HTTP 401/403 (session expired), other 4xx (client
   error) and 5xx (server error), plus transport failures.
2. **User-visible notifications** - toast / alert / modal text the page shows.

``wait_first`` races these signals against the "result is ready" condition and
returns whichever happens first. Because it never depends on portal-specific
markup, the same pattern works across different targets.

This is a *reference sample* for a Playwright (sync API) page. The URL prefix
and selectors below are placeholders.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional, Sequence
from urllib.parse import urlsplit


class EventKind(str, Enum):
    SESSION_EXPIRED = "session_expired"  # 401 / 403
    SERVER_ERROR = "server_error"        # 5xx
    CLIENT_ERROR = "client_error"        # other 4xx
    TRANSPORT_ERROR = "transport_error"  # DNS / proxy / connection reset


class Outcome(str, Enum):
    READY = "ready"
    SESSION_EXPIRED = "session_expired"
    SERVER_ERROR = "server_error"
    CLIENT_ERROR = "client_error"
    TRANSPORT_ERROR = "transport_error"
    NOTIFICATION = "notification"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class NetworkEvent:
    kind: EventKind
    status: int
    path: str  # path only - query strings are dropped so they never reach logs


@dataclass(frozen=True)
class WaitResult:
    outcome: Outcome
    detail: str = ""


def classify_status(status: int) -> Optional[EventKind]:
    """Map an HTTP status code to an event kind (``None`` means "not a problem")."""
    if status in (401, 403):
        return EventKind.SESSION_EXPIRED
    if status >= 500:
        return EventKind.SERVER_ERROR
    if 400 <= status < 500:
        return EventKind.CLIENT_ERROR
    return None


class NetworkListener:
    """Collect problem responses for requests under ``watch_prefixes``."""

    def __init__(self, page, watch_prefixes: Sequence[str] = ("/api/",)) -> None:
        self._prefixes = tuple(watch_prefixes)
        self._events: List[NetworkEvent] = []
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)

    def _watched(self, url: str) -> Optional[str]:
        path = urlsplit(url).path
        return path if path.startswith(self._prefixes) else None

    def _on_response(self, response) -> None:
        try:
            path = self._watched(response.url)
            kind = classify_status(response.status) if path else None
            if kind:
                self._events.append(NetworkEvent(kind, response.status, path))
        except Exception:
            pass  # a listener must never raise into the browser event loop

    def _on_request_failed(self, request) -> None:
        try:
            path = self._watched(request.url)
            if path and "ERR_ABORTED" not in str(request.failure or ""):
                self._events.append(NetworkEvent(EventKind.TRANSPORT_ERROR, 0, path))
        except Exception:
            pass

    def mark(self) -> int:
        """Bookmark the current position; only later events are considered."""
        return len(self._events)

    def first_event_since(self, mark: int) -> Optional[NetworkEvent]:
        return self._events[mark] if len(self._events) > mark else None


def read_visible_notification(
    page, selectors: Sequence[str] = (".toast", ".alert", "[role=alert]", ".modal.show .modal-body")
) -> Optional[str]:
    """Return the text of the first visible notification element, if any."""
    script = """
        (selectors) => {
          for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
              const box = el.getBoundingClientRect();
              const text = (el.innerText || '').trim();
              if (box.width > 0 && box.height > 0 && text) return text;
            }
          }
          return null;
        }
    """
    try:
        return page.evaluate(script, list(selectors))
    except Exception:
        return None  # page is navigating; the next poll will succeed


_EVENT_TO_OUTCOME = {
    EventKind.SESSION_EXPIRED: Outcome.SESSION_EXPIRED,
    EventKind.SERVER_ERROR: Outcome.SERVER_ERROR,
    EventKind.CLIENT_ERROR: Outcome.CLIENT_ERROR,
    EventKind.TRANSPORT_ERROR: Outcome.TRANSPORT_ERROR,
}


def wait_first(
    page,
    listener: NetworkListener,
    mark: int,
    is_ready: Callable[[], bool],
    timeout_s: float = 30.0,
    poll_s: float = 0.25,
) -> WaitResult:
    """Wait for the first of: result ready, network problem, notification, timeout.

    Typical use::

        mark = listener.mark()
        page.click("#submit")
        result = wait_first(page, listener, mark, lambda: page.locator("table").count() > 0)
        if result.outcome is Outcome.SESSION_EXPIRED:
            relogin()
        elif result.outcome is not Outcome.READY:
            log.warning("skipping item: %s (%s)", result.outcome.value, result.detail)
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if is_ready():
            return WaitResult(Outcome.READY)

        event = listener.first_event_since(mark)
        if event:
            return WaitResult(_EVENT_TO_OUTCOME[event.kind], f"HTTP {event.status} {event.path}")

        message = read_visible_notification(page)
        if message:
            return WaitResult(Outcome.NOTIFICATION, message)

        if time.monotonic() >= deadline:
            return WaitResult(Outcome.TIMEOUT, f"no response within {timeout_s:.0f}s")

        # Playwright's sync API delivers events while its own calls run, so the
        # short sleep below also lets the listener receive new responses.
        page.wait_for_timeout(int(poll_s * 1000))
