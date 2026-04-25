"""User-level app settings stored at ~/.agent-cad/settings.json.

Keep this small — settings the user actually changes from the UI. Per-
project state lives in the project itself.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_DIR = Path.home() / ".agent-cad"
LEGACY_CONFIG_DIR = Path.home() / ".cc-cad"


def _migrate_legacy_config_dir() -> None:
    """One-time migration from .cc-cad → .agent-cad. The project was
    renamed; existing installs keep their settings + projects in place."""
    if CONFIG_DIR.exists() or not LEGACY_CONFIG_DIR.exists():
        return
    try:
        LEGACY_CONFIG_DIR.rename(CONFIG_DIR)
    except OSError:
        # Cross-device or in-use; leave the legacy dir alone. Nothing on
        # disk is destroyed; the user will just see fresh defaults.
        return
    # Rewrite any leftover path references inside settings.json so the
    # default-project-dir still points at the freshly-renamed folder.
    moved_settings = CONFIG_DIR / "settings.json"
    if not moved_settings.exists():
        return
    try:
        txt = moved_settings.read_text(encoding="utf-8")
    except OSError:
        return
    new = txt.replace(str(LEGACY_CONFIG_DIR), str(CONFIG_DIR))
    # JSON-escaped form (Windows paths with double backslashes)
    new = new.replace(
        str(LEGACY_CONFIG_DIR).replace("\\", "\\\\"),
        str(CONFIG_DIR).replace("\\", "\\\\"),
    )
    if new != txt:
        try:
            moved_settings.write_text(new, encoding="utf-8")
        except OSError:
            pass


_migrate_legacy_config_dir()

SETTINGS_PATH = CONFIG_DIR / "settings.json"
DEFAULT_PROJECT_DIR = CONFIG_DIR / "projects"
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_EFFORT = "medium"

KNOWN_MODELS = [
    {"id": "claude-opus-4-7",          "label": "Claude Opus 4.7",   "tier": "highest quality"},
    {"id": "claude-sonnet-4-6",        "label": "Claude Sonnet 4.6", "tier": "balanced"},
    {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5",  "tier": "fastest / cheapest"},
]

KNOWN_EFFORTS = [
    {"id": "low",    "label": "Low — fast, fewer checks"},
    {"id": "medium", "label": "Medium — balanced (default)"},
    {"id": "high",   "label": "High — careful, slower"},
    {"id": "max",    "label": "Max — most thorough, slowest"},
]


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    default_project_dir: str = str(DEFAULT_PROJECT_DIR)
    effort: str = DEFAULT_EFFORT

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
        default_project_dir=d.get("default_project_dir", str(DEFAULT_PROJECT_DIR)),
        effort=d.get("effort", DEFAULT_EFFORT),
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
