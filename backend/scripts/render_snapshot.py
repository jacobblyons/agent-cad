"""Render a PNG of a CADQuery model from a chosen camera, via the same
VTK pipeline the agent's snapshot tool uses.

Wraps `app.cad.script_runner.snapshot` so the model is loaded and rendered
in a clean subprocess (matches the agent and viewer behaviour exactly).

Examples:
    # Single-script model, isometric preset:
    python backend/scripts/render_snapshot.py path/to/widget.py --view iso

    # A project directory + named object, top-down:
    python backend/scripts/render_snapshot.py ~/.agent-cad/projects/p1 \\
        --object base_plate --view top

    # Custom camera (CADQuery coords, mm, +Z up):
    python backend/scripts/render_snapshot.py path/to/widget.py \\
        --position 200 -180 140 --target 0 0 30 --up 0 0 1
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import uuid
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))

from app.cad.script_runner import snapshot  # noqa: E402

PRESETS = ("iso", "front", "back", "left", "right", "top", "bottom")
IMPORT_EXTS = {
    ".step", ".stp", ".iges", ".igs", ".brep", ".brp",
    ".stl", ".glb", ".gltf", ".3mf",
}


def _project_sketches(proj: Path) -> list[dict] | None:
    d = proj / "sketches"
    if not d.is_dir():
        return None
    out: list[dict] = []
    for py in sorted(d.glob("*.py")):
        out.append({
            "name": py.stem,
            "script": str(py),
            "params": str(d / f"{py.stem}.params.json"),
        })
    return out or None


def _project_imports(proj: Path) -> list[dict] | None:
    d = proj / "imports"
    if not d.is_dir():
        return None
    out: list[dict] = []
    for f in sorted(d.iterdir()):
        if f.is_file() and f.suffix.lower() in IMPORT_EXTS:
            out.append({"name": f.stem, "path": str(f)})
    return out or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render a PNG snapshot of a CADQuery model from a chosen "
                    "camera, using the same backend pipeline as the agent.",
    )
    ap.add_argument(
        "input",
        help="Path to a .py script (defining a top-level `model`), OR a "
             "project directory containing objects/<name>.py (use --object).",
    )
    ap.add_argument(
        "--object", dest="object_name",
        help="When input is a project directory, the object name "
             "(filename stem under objects/).",
    )
    ap.add_argument(
        "--view", default="iso",
        help=f"Preset view name. One of: {', '.join(PRESETS)}. Default: iso.",
    )
    ap.add_argument(
        "--position", nargs=3, type=float, metavar=("X", "Y", "Z"),
        help="Custom camera position in CADQuery coords (mm, +Z up). "
             "Overrides --view.",
    )
    ap.add_argument(
        "--target", nargs=3, type=float, metavar=("X", "Y", "Z"),
        default=[0.0, 0.0, 0.0],
        help="Camera target / orbit center (default: 0 0 0). Used with --position.",
    )
    ap.add_argument(
        "--up", nargs=3, type=float, metavar=("X", "Y", "Z"),
        default=[0.0, 0.0, 1.0],
        help="Camera up vector (default: 0 0 1). Used with --position.",
    )
    ap.add_argument("--width", type=int, default=900)
    ap.add_argument("--height", type=int, default=700)
    ap.add_argument(
        "--params",
        help="Optional path to a params JSON file injected as the script's "
             "`params` global. If unspecified, the CLI looks for "
             "<script>.params.json (single-script mode) or "
             "objects/<name>.params.json (project mode).",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument(
        "--out",
        help="Output PNG path. Defaults to a fresh temp file; the final "
             "path is always printed to stdout on success.",
    )
    args = ap.parse_args(argv)

    inp = Path(args.input).resolve()
    if not inp.exists():
        print(f"input does not exist: {inp}", file=sys.stderr)
        return 2

    sketches: list[dict] | None
    imports: list[dict] | None
    if inp.is_dir():
        if not args.object_name:
            print("input is a directory; --object NAME is required to pick one",
                  file=sys.stderr)
            return 2
        script_path = inp / "objects" / f"{args.object_name}.py"
        if not script_path.exists():
            print(f"object script not found: {script_path}", file=sys.stderr)
            return 2
        params_path = (Path(args.params).resolve() if args.params
                       else inp / "objects" / f"{args.object_name}.params.json")
        sketches = _project_sketches(inp)
        imports = _project_imports(inp)
        cwd = inp
    else:
        script_path = inp
        params_path = (Path(args.params).resolve() if args.params
                       else inp.parent / f"{inp.stem}.params.json")
        sketches = None
        imports = None
        cwd = inp.parent

    if args.position:
        view: dict = {
            "position": list(args.position),
            "target": list(args.target),
            "up": list(args.up),
        }
    else:
        if args.view not in PRESETS:
            print(f"--view must be one of: {', '.join(PRESETS)}", file=sys.stderr)
            return 2
        view = {"preset": args.view}

    out_path = (Path(args.out).resolve() if args.out
                else Path(tempfile.gettempdir()) / f"cad-snapshot-{uuid.uuid4().hex[:8]}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    res = snapshot(
        script_path, params_path, view,
        cwd=cwd,
        width=args.width, height=args.height,
        timeout=args.timeout,
        sketches=sketches, imports=imports,
    )
    if not res.ok:
        print(f"render failed: {res.error}", file=sys.stderr)
        if res.stderr:
            print(res.stderr, file=sys.stderr)
        return 1

    out_path.write_bytes(res.png_bytes or b"")
    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
