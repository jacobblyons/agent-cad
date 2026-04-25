"""Subprocess entrypoint: load a CADQuery script and render a PNG snapshot.

VTK's render window isn't reliably thread-safe on Windows — calling it
from the agent's worker thread can hang or crash. Pushing the work into a
clean subprocess sidesteps the whole issue.

Argv:
    1 = script_path  (path to the .py file defining `model`)
    2 = params_path  (path to params JSON; may be missing/empty)
    3 = view JSON    (e.g. {"preset": "iso"} or {"position":[...], "target":[...], "up":[...]})
    4 = width
    5 = height
    6 = output PNG path
    7 = output JSON path  (always written; ok=True/False)
"""
from __future__ import annotations

import json
import runpy
import sys
import traceback
from pathlib import Path


def main() -> int:
    script_path = Path(sys.argv[1])
    params_path = Path(sys.argv[2])
    view = json.loads(sys.argv[3])
    width = int(sys.argv[4])
    height = int(sys.argv[5])
    png_out = Path(sys.argv[6])
    json_out = Path(sys.argv[7])

    result: dict = {"ok": False, "error": None}
    try:
        params: dict = {}
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError as e:
                result["error"] = f"params file is invalid: {e}"
                json_out.write_text(json.dumps(result), encoding="utf-8")
                return 0

        if not script_path.exists():
            result["error"] = f"script not found: {script_path}"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0

        globs = runpy.run_path(str(script_path), init_globals={"params": params})
        model = globs.get("model")
        if model is None:
            result["error"] = f"{script_path.name} finished without defining `model`"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0

        from app.cad.snapshot import render_png
        png = render_png(model, view, width=width, height=height)
        png_out.write_bytes(png)
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["trace"] = traceback.format_exc()

    json_out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
