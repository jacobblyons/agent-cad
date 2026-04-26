"""Cross-thread permission registry.

The Claude Agent SDK fires a `can_use_tool` callback in its asyncio
event loop on the agent's worker thread. To ask the user — whose
clicks land via JsApi calls on the main webview thread — we need a
thread-safe handoff. This module is that handoff: the agent's
callback calls `request()` to register a pending permission and waits
on a `threading.Event`; the JsApi `permission_resolve` endpoint calls
`resolve()` from any thread to set the result and unblock the wait.

Auto-allow rules (e.g. always allow CAD tools, always allow Read) live
in the runner — this module is just the queue.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass


@dataclass
class PermissionResult:
    approved: bool
    message: str = ""


class PermissionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, PermissionResult] = {}

    def request(self) -> tuple[str, threading.Event]:
        """Register a new pending permission. Returns (request_id, event)
        — the caller should `event.wait(timeout=...)` then call
        `take_result(request_id)` once it returns."""
        request_id = uuid.uuid4().hex
        ev = threading.Event()
        with self._lock:
            self._events[request_id] = ev
        return request_id, ev

    def resolve(self, request_id: str, approved: bool, message: str = "") -> bool:
        """Mark a pending permission as resolved. Returns False if the
        request_id is unknown (already resolved or never registered)."""
        with self._lock:
            ev = self._events.get(request_id)
            if ev is None:
                return False
            self._results[request_id] = PermissionResult(approved=approved, message=message)
        ev.set()
        return True

    def take_result(self, request_id: str) -> PermissionResult | None:
        """Pop and return the resolved result. Caller is responsible for
        cleaning up the corresponding event entry, which we do too."""
        with self._lock:
            self._events.pop(request_id, None)
            return self._results.pop(request_id, None)

    def cancel(self, request_id: str) -> None:
        """Drop a request without setting a result — useful if the caller
        gives up (e.g. the agent turn was cancelled)."""
        with self._lock:
            self._events.pop(request_id, None)
            self._results.pop(request_id, None)


# Process-wide store. The agent runner and the JsApi both reach in here.
store = PermissionStore()
