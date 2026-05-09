"""Push channel from backend to webview.

Mirror of apps/cad/backend/app/events.py with a different CustomEvent
prefix ('agentslicer:' instead of 'agentcad:') so the two apps' events
can't collide if they ever share a frontend (they shouldn't, but
defense in depth). Phase 2 of the v0.2.0 split extracts both into
shared/python/events.py with the prefix configurable.
"""
from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class Event:
    channel: str
    payload: Any


class EventBus:
    def __init__(self, prefix: str = "agentslicer") -> None:
        self._q: queue.Queue[Event | None] = queue.Queue()
        self._window = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._prefix = prefix

    def attach(self, window) -> None:
        self._window = window
        self._thread = threading.Thread(target=self._drain, name="event-drain", daemon=True)
        self._thread.start()

    def emit(self, channel: str, payload: Any) -> None:
        self._q.put(Event(channel=channel, payload=payload))

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)

    def _drain(self) -> None:
        while not self._stop.is_set():
            ev = self._q.get()
            if ev is None:
                return
            if self._window is None:
                continue
            payload_json = json.dumps(ev.payload)
            js = (
                f"window.dispatchEvent(new CustomEvent("
                f"'{self._prefix}:{ev.channel}', {{detail: {payload_json}}}));"
            )
            try:
                self._window.evaluate_js(js)
            except Exception:
                pass


bus = EventBus()
