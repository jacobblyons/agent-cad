"""Per-project print-phase state.

While the print phase is active for a project, the user has chosen a
preset, the agent has applied zero or more overrides, and zero or one
slice has been produced. This file defines the in-memory shape of that
state plus a small registry keyed by project id. The state is *not*
persisted to disk — restarting the app drops back into CAD phase.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .presets import DEFAULT_PRESET
from .slicers import SliceOverride, SliceResult


@dataclass
class PrintSession:
    """Live state for a project that's currently in the print phase."""
    project_id: str
    preset: str = DEFAULT_PRESET
    overrides: list[SliceOverride] = field(default_factory=list)
    last_slice: SliceResult | None = None
    last_export_path: str | None = None  # the .3mf we exported and fed the slicer
    printer_id: str | None = None        # id of the printer we'll send to
    last_send_message: str = ""
    last_send_ok: bool | None = None
    started_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "project_id": self.project_id,
            "preset": self.preset,
            "overrides": [o.to_json() for o in self.overrides],
            "last_slice": self.last_slice.to_json() if self.last_slice else None,
            "last_export_path": self.last_export_path,
            "printer_id": self.printer_id,
            "last_send_message": self.last_send_message,
            "last_send_ok": self.last_send_ok,
            "started_at": self.started_at,
        }


class PhaseState:
    """Tracks which projects are in the print phase, plus their state.

    Stored as a singleton-ish on the JsApi instance — one PhaseState
    serves every open project tab. CAD phase is the implicit default;
    a project is in print phase iff it has an entry here.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, PrintSession] = {}

    def is_active(self, project_id: str) -> bool:
        return project_id in self._sessions

    def get(self, project_id: str) -> PrintSession | None:
        return self._sessions.get(project_id)

    def enter(self, project_id: str) -> PrintSession:
        if project_id not in self._sessions:
            self._sessions[project_id] = PrintSession(project_id=project_id)
        return self._sessions[project_id]

    def leave(self, project_id: str) -> None:
        self._sessions.pop(project_id, None)

    def update(self, project_id: str, **fields) -> PrintSession | None:
        s = self._sessions.get(project_id)
        if s is None:
            return None
        for k, v in fields.items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s
