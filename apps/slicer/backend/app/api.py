"""JS API exposed to the webview via pywebview's `js_api`.

Skeleton for Phase 3. Methods that a real plate manager will need are
declared as stubs that just return a marker. Phase 4 fleshes out the
3MF model + plate state and wires real values through these methods.
"""
from __future__ import annotations

from typing import Any

from . import settings


class JsApi:
    """Methods on this object are reachable from the renderer as
    `pywebview.api.<name>(...)`.

    Pythonic snake_case here surfaces as snake_case on the JS side.
    """

    # --- Settings ---------------------------------------------------

    def get_settings(self) -> dict:
        return settings.load().to_json()

    def set_settings(self, **fields: Any) -> dict:
        return settings.update(**fields).to_json()

    # --- Project / plate stubs (Phase 4 fills these in) -------------

    def open_project(self, path: str | None = None) -> dict:
        return {"ok": False, "error": "open_project not implemented yet (Phase 4)"}

    def list_plates(self) -> list:
        return []

    def list_models(self, plate: int | None = None) -> list:
        return []

    def set_active_plate(self, index: int) -> dict:
        return {"ok": False, "error": "set_active_plate not implemented yet (Phase 4)"}

    def add_model(self, file_path: str, plate: int | None = None) -> dict:
        return {"ok": False, "error": "add_model not implemented yet (Phase 4)"}

    # --- Health / heartbeat ----------------------------------------

    def ping(self) -> str:
        """Cheap end-to-end check: webview can call into Python."""
        return "pong"
