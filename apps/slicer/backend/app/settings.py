"""User-level settings for Agent Slicer.

Stored at ~/.agent-cad/slicer.json — sibling to the cad app's settings.
The plan from docs/agent-slicer.md is to keep model+effort in a shared
~/.agent-cad/agent.json eventually; for the skeleton each app reads
its own file. Phase 2 will harmonize.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".agent-cad"
SETTINGS_PATH = CONFIG_DIR / "slicer.json"
DEFAULT_PROJECT_DIR = CONFIG_DIR / "slicer-projects"
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_EFFORT = "medium"


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    default_project_dir: str = str(DEFAULT_PROJECT_DIR)
    # Slicer-specific configuration moves here in Phase 8 (printer LAN
    # configs, slicer CLI path, default preset). Stub for now so the
    # window has something to read.
    printers: list = field(default_factory=list)
    default_printer_id: str = ""
    bambu_studio_cli_path: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def load() -> Settings:
    if not SETTINGS_PATH.exists():
        s = Settings()
        save(s)
        return s
    try:
        d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return Settings()
    return Settings(
        model=d.get("model", DEFAULT_MODEL),
        effort=d.get("effort", DEFAULT_EFFORT),
        default_project_dir=d.get("default_project_dir", str(DEFAULT_PROJECT_DIR)),
        printers=d.get("printers") or [],
        default_printer_id=str(d.get("default_printer_id", "") or ""),
        bambu_studio_cli_path=str(d.get("bambu_studio_cli_path", "") or ""),
    )


def save(s: Settings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s.to_json(), indent=2), encoding="utf-8")


def update(**fields) -> Settings:
    s = load()
    for k, v in fields.items():
        if hasattr(s, k):
            setattr(s, k, v)
    save(s)
    return s
