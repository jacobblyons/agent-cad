"""Push channel from backend to webview.

Background work (agent runner, executor) cannot call into the webview
directly. They enqueue events here; a single drain thread forwards them
to the JS side via window.evaluate_js.
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
    def __init__(self) -> None:
        self._q: queue.Queue[Event | None] = queue.Queue()
        self._window = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

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
            # Dispatch a CustomEvent on window so React can listen via addEventListener.
            js = (
                f"window.dispatchEvent(new CustomEvent("
                f"'agentcad:{ev.channel}', {{detail: {payload_json}}}));"
            )
            try:
                self._window.evaluate_js(js)
            except Exception:
                # Window may be closing; drop the event.
                pass


bus = EventBus()
