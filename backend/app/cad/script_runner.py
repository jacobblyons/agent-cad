"""Runs a CADQuery script in a clean subprocess and returns geometry + meta.

Why a subprocess: the script is arbitrary Python written by the agent. A
bad script (infinite loop, segfault in OCCT, runaway memory) would
otherwise take down the host. The subprocess gives us a hard timeout.

Stdout protocol: a single JSON object on the last line. Glb is written to a
temp file path that the parent reads, so we never push hundreds of KB of
base64 through a stdio pipe.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

# Path to this file's directory — used by the subprocess wrapper.
_PKG_ROOT = Path(__file__).resolve().parents[2]   # .../backend


@dataclass
class RunResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    glb_b64: str | None = None
    meta: dict | None = None
    topology: dict | None = None

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "meta": self.meta,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


def _write_sketches_manifest(sketches: list[dict] | None) -> str:
    """Serialize a sketches manifest to a temp JSON; return its path or '-'."""
    if not sketches:
        return "-"
    p = Path(tempfile.gettempdir()) / f"agentcad-sketches-{uuid.uuid4().hex}.json"
    p.write_text(json.dumps([
        {
            "name": s["name"],
            "script": str(Path(s["script"]).resolve()),
            "params": str(Path(s["params"]).resolve()),
        }
        for s in sketches
    ]), encoding="utf-8")
    return str(p)


def _write_imports_manifest(imports: list[dict] | None) -> str:
    """Serialize an imports manifest to a temp JSON; return its path or '-'."""
    if not imports:
        return "-"
    p = Path(tempfile.gettempdir()) / f"agentcad-imports-{uuid.uuid4().hex}.json"
    p.write_text(json.dumps([
        {
            "name": i["name"],
            "path": str(Path(i["path"]).resolve()),
        }
        for i in imports
    ]), encoding="utf-8")
    return str(p)


def run(script_path: Path, params_path: Path, *, cwd: Path | None = None,
        timeout: float = 30.0, deflection: float = 0.1,
        sketches: list[dict] | None = None,
        imports: list[dict] | None = None) -> RunResult:
    """Run the given script in a subprocess with `params` injected.

    `sketches`: optional list of {"name", "script", "params"} entries that
    are loaded BEFORE the object script so they're available as a
    `sketches` dict (name → placed cq.Workplane) to the script.

    `imports`: optional list of {"name", "path"} entries pointing at STEP
    files; each is loaded and injected as an entry in `imports` dict
    (name → cq.Workplane), so the script can boolean against / measure
    off user-supplied reference geometry.
    """
    script_path = Path(script_path).resolve()
    params_path = Path(params_path).resolve()
    cwd = Path(cwd).resolve() if cwd else script_path.parent
    glb_path = Path(tempfile.gettempdir()) / f"agentcad-{uuid.uuid4().hex}.glb"
    json_path = Path(tempfile.gettempdir()) / f"agentcad-{uuid.uuid4().hex}.json"
    sketches_arg = _write_sketches_manifest(sketches)
    imports_arg = _write_imports_manifest(imports)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_PKG_ROOT), env.get("PYTHONPATH", "")],
    ).strip(os.pathsep)

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "app.cad._script_worker",
                str(script_path), str(params_path),
                str(glb_path), str(json_path), str(deflection),
                sketches_arg, imports_arg,
            ],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as e:
        return RunResult(
            ok=False,
            error=f"script exceeded {timeout}s timeout",
            stdout=(e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=(e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
        )

    if not json_path.exists():
        return RunResult(
            ok=False,
            error=f"runner produced no result file (exit {proc.returncode})",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    try:
        result = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return RunResult(ok=False, error=f"could not parse runner result: {e}",
                         stdout=proc.stdout, stderr=proc.stderr)
    finally:
        try:
            json_path.unlink()
        except OSError:
            pass

    glb_b64 = None
    if result.get("ok") and glb_path.exists():
        import base64
        glb_b64 = base64.b64encode(glb_path.read_bytes()).decode("ascii")
    if glb_path.exists():
        try:
            glb_path.unlink()
        except OSError:
            pass

    return RunResult(
        ok=result.get("ok", False),
        error=result.get("error"),
        meta=result.get("meta"),
        topology=result.get("topology"),
        stdout=proc.stdout,
        stderr=proc.stderr,
        glb_b64=glb_b64,
    )


@dataclass
class SnapshotResult:
    ok: bool
    png_bytes: bytes | None = None
    error: str | None = None
    stderr: str = ""


@dataclass
class SketchResult:
    """Tessellated polylines for a sketch, ready to ship to the viewer."""
    ok: bool
    error: str | None = None
    plane: dict | None = None
    polylines: list[dict] | None = None
    bbox: dict | None = None
    stderr: str = ""


@dataclass
class ImportResult:
    """Tessellated GLB + topology for a STEP import, mirroring RunResult."""
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    glb_b64: str | None = None
    meta: dict | None = None
    topology: dict | None = None


def tessellate_import(source_path: Path, *, cwd: Path | None = None,
                      timeout: float = 60.0,
                      deflection: float = 0.1) -> "ImportResult":
    """Load a STEP file in a subprocess and tessellate to GLB."""
    source_path = Path(source_path).resolve()
    cwd = Path(cwd).resolve() if cwd else source_path.parent
    glb_path = Path(tempfile.gettempdir()) / f"agentcad-import-{uuid.uuid4().hex}.glb"
    json_path = Path(tempfile.gettempdir()) / f"agentcad-import-{uuid.uuid4().hex}.json"

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_PKG_ROOT), env.get("PYTHONPATH", "")],
    ).strip(os.pathsep)

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "app.cad._import_worker",
                str(source_path), str(glb_path), str(json_path), str(deflection),
            ],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as e:
        return ImportResult(
            ok=False,
            error=f"import tessellation exceeded {timeout}s",
            stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
        )

    if not json_path.exists():
        return ImportResult(
            ok=False,
            error=f"import worker produced no result file (exit {proc.returncode})",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    try:
        result = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return ImportResult(ok=False, error=f"could not parse import result: {e}",
                            stdout=proc.stdout, stderr=proc.stderr)
    finally:
        try:
            json_path.unlink()
        except OSError:
            pass

    glb_b64 = None
    if result.get("ok") and glb_path.exists():
        import base64
        glb_b64 = base64.b64encode(glb_path.read_bytes()).decode("ascii")
    if glb_path.exists():
        try:
            glb_path.unlink()
        except OSError:
            pass

    return ImportResult(
        ok=bool(result.get("ok")),
        error=result.get("error"),
        meta=result.get("meta"),
        topology=result.get("topology"),
        stdout=proc.stdout,
        stderr=proc.stderr,
        glb_b64=glb_b64,
    )


def tessellate_sketch(script_path: Path, params_path: Path, *,
                      cwd: Path | None = None, timeout: float = 20.0,
                      tol: float = 0.5) -> "SketchResult":
    """Run a sketch script in a subprocess and return its wires as 3D
    polylines."""
    script_path = Path(script_path).resolve()
    params_path = Path(params_path).resolve()
    cwd = Path(cwd).resolve() if cwd else script_path.parent
    json_path = Path(tempfile.gettempdir()) / f"agentcad-sketch-{uuid.uuid4().hex}.json"

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_PKG_ROOT), env.get("PYTHONPATH", "")],
    ).strip(os.pathsep)

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "app.cad._sketch_worker",
                str(script_path), str(params_path), str(json_path), str(tol),
            ],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as e:
        return SketchResult(
            ok=False,
            error=f"sketch tessellation exceeded {timeout}s",
            stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
        )

    if not json_path.exists():
        return SketchResult(
            ok=False,
            error=f"sketch worker produced no result file (exit {proc.returncode})",
            stderr=proc.stderr,
        )
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return SketchResult(ok=False, error=f"could not parse sketch result: {e}",
                            stderr=proc.stderr)
    finally:
        try:
            json_path.unlink()
        except OSError:
            pass

    return SketchResult(
        ok=bool(data.get("ok")),
        error=data.get("error"),
        plane=data.get("plane"),
        polylines=data.get("polylines"),
        bbox=data.get("bbox"),
        stderr=proc.stderr,
    )


@dataclass
class ExportResult:
    ok: bool
    error: str | None = None
    stderr: str = ""
    path: str | None = None


def export_models(items: list[dict], dest: Path, *, cwd: Path | None = None,
                  timeout: float = 60.0,
                  sketches: list[dict] | None = None,
                  imports: list[dict] | None = None) -> "ExportResult":
    """Run each item's script and union the resulting models into one
    file at `dest`. Format is inferred from `dest`'s extension (.stl /
    .step / .brep). Used for both per-object exports (items has length
    1) and project-wide exports (items has every visible object).
    """
    spec = {
        "items": items,
        "post": {"kind": "export", "path": str(Path(dest).resolve())},
    }
    res = scene(spec, cwd=cwd, timeout=timeout, sketches=sketches, imports=imports)
    return ExportResult(
        ok=res.ok,
        error=res.error,
        stderr=res.stderr,
        path=str(dest) if res.ok else None,
    )


def scene(spec: dict, *, cwd: Path | None = None,
          timeout: float = 45.0,
          sketches: list[dict] | None = None,
          imports: list[dict] | None = None) -> "SnapshotResult":
    """Render a multi-item scene via the scene worker subprocess.

    spec shape: see _scene_worker.main docstring.

    `sketches` / `imports`: optional manifest entries shared across every
    item in the scene; written to temp JSON files and referenced by path
    in the spec so long lists don't bloat the spec file.
    """
    cwd = Path(cwd).resolve() if cwd else Path.cwd()
    spec_path = Path(tempfile.gettempdir()) / f"agentcad-scene-{uuid.uuid4().hex}.json"
    png_path = Path(tempfile.gettempdir()) / f"agentcad-scene-{uuid.uuid4().hex}.png"
    json_path = Path(tempfile.gettempdir()) / f"agentcad-scene-{uuid.uuid4().hex}.json"

    spec = dict(spec)
    sketches_arg = _write_sketches_manifest(sketches)
    if sketches_arg != "-":
        spec["sketches_manifest"] = sketches_arg
    imports_arg = _write_imports_manifest(imports)
    if imports_arg != "-":
        spec["imports_manifest"] = imports_arg

    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_PKG_ROOT), env.get("PYTHONPATH", "")],
    ).strip(os.pathsep)

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "app.cad._scene_worker",
                str(spec_path), str(png_path), str(json_path),
            ],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as e:
        return SnapshotResult(ok=False, error=f"scene render exceeded {timeout}s",
                              stderr=(e.stderr or "") if isinstance(e.stderr, str) else "")

    stderr = proc.stderr or ""
    if not json_path.exists():
        return SnapshotResult(
            ok=False,
            error=f"scene worker produced no result file (exit {proc.returncode})",
            stderr=stderr,
        )
    try:
        result = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return SnapshotResult(ok=False, error=f"could not parse worker result: {e}",
                              stderr=stderr)
    finally:
        try:
            json_path.unlink()
        except OSError:
            pass

    # Export mode never writes a PNG — the worker dropped a real model
    # file (.stl / .step / .brep) at the user-chosen path instead.
    is_export = isinstance(spec.get("post"), dict) and spec["post"].get("kind") == "export"

    if not result.get("ok"):
        if png_path.exists():
            try:
                png_path.unlink()
            except OSError:
                pass
        return SnapshotResult(ok=False, error=result.get("error") or "scene render failed",
                              stderr=stderr)

    if is_export:
        return SnapshotResult(ok=True, png_bytes=None, stderr=stderr)

    if not png_path.exists():
        return SnapshotResult(ok=False, error="worker reported ok but no PNG was written",
                              stderr=stderr)

    png_bytes = png_path.read_bytes()
    try:
        png_path.unlink()
    except OSError:
        pass
    return SnapshotResult(ok=True, png_bytes=png_bytes, stderr=stderr)


def snapshot(script_path: Path, params_path: Path, view: dict, *,
             cwd: Path | None = None, width: int = 800, height: int = 600,
             timeout: float = 30.0,
             sketches: list[dict] | None = None,
             imports: list[dict] | None = None) -> SnapshotResult:
    """Render a PNG snapshot of the given script via the snapshot worker subprocess.

    `sketches` / `imports`: optional manifest entries — same shape as
    `run()`. The snapshot worker injects them so an object script that
    consumes sketches or references imported geometry renders correctly
    here too.
    """
    script_path = Path(script_path).resolve()
    params_path = Path(params_path).resolve()
    cwd = Path(cwd).resolve() if cwd else script_path.parent
    png_path = Path(tempfile.gettempdir()) / f"agentcad-snap-{uuid.uuid4().hex}.png"
    json_path = Path(tempfile.gettempdir()) / f"agentcad-snap-{uuid.uuid4().hex}.json"
    sketches_arg = _write_sketches_manifest(sketches)
    imports_arg = _write_imports_manifest(imports)

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_PKG_ROOT), env.get("PYTHONPATH", "")],
    ).strip(os.pathsep)

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "app.cad._snapshot_worker",
                str(script_path), str(params_path),
                json.dumps(view),
                str(int(width)), str(int(height)),
                str(png_path), str(json_path),
                sketches_arg, imports_arg,
            ],
            capture_output=True, text=True, timeout=timeout, env=env,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as e:
        return SnapshotResult(ok=False, error=f"snapshot exceeded {timeout}s",
                              stderr=(e.stderr or "") if isinstance(e.stderr, str) else "")

    stderr = proc.stderr or ""
    if not json_path.exists():
        return SnapshotResult(
            ok=False,
            error=f"snapshot worker produced no result file (exit {proc.returncode})",
            stderr=stderr,
        )
    try:
        result = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return SnapshotResult(ok=False, error=f"could not parse worker result: {e}",
                              stderr=stderr)
    finally:
        try:
            json_path.unlink()
        except OSError:
            pass

    if not result.get("ok"):
        if png_path.exists():
            try:
                png_path.unlink()
            except OSError:
                pass
        return SnapshotResult(ok=False, error=result.get("error") or "snapshot failed",
                              stderr=stderr)

    if not png_path.exists():
        return SnapshotResult(ok=False, error="worker reported ok but no PNG was written",
                              stderr=stderr)

    png_bytes = png_path.read_bytes()
    try:
        png_path.unlink()
    except OSError:
        pass
    return SnapshotResult(ok=True, png_bytes=png_bytes, stderr=stderr)
