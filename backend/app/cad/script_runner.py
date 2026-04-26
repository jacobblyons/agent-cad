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


def run(script_path: Path, params_path: Path, *, cwd: Path | None = None,
        timeout: float = 30.0, deflection: float = 0.1,
        sketches: list[dict] | None = None) -> RunResult:
    """Run the given script in a subprocess with `params` injected.

    `sketches`: optional list of {"name", "script", "params"} entries that
    are loaded BEFORE the object script so they're available as a
    `sketches` dict (name → placed cq.Workplane) to the script.
    """
    script_path = Path(script_path).resolve()
    params_path = Path(params_path).resolve()
    cwd = Path(cwd).resolve() if cwd else script_path.parent
    glb_path = Path(tempfile.gettempdir()) / f"agentcad-{uuid.uuid4().hex}.glb"
    json_path = Path(tempfile.gettempdir()) / f"agentcad-{uuid.uuid4().hex}.json"
    sketches_arg = "-"
    sketches_path: Path | None = None
    if sketches:
        sketches_path = (
            Path(tempfile.gettempdir()) / f"agentcad-sketches-{uuid.uuid4().hex}.json"
        )
        # Resolve all paths relative to the worker's view of the filesystem.
        sketches_path.write_text(json.dumps([
            {
                "name": s["name"],
                "script": str(Path(s["script"]).resolve()),
                "params": str(Path(s["params"]).resolve()),
            }
            for s in sketches
        ]), encoding="utf-8")
        sketches_arg = str(sketches_path)

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
                sketches_arg,
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


def scene(spec: dict, *, cwd: Path | None = None,
          timeout: float = 45.0,
          sketches: list[dict] | None = None) -> "SnapshotResult":
    """Render a multi-item scene via the scene worker subprocess.

    spec shape: see _scene_worker.main docstring.

    `sketches`: optional manifest entries shared across every item in the
    scene; written to a temp JSON and referenced by path in the spec so a
    long sketches list doesn't bloat the spec file.
    """
    cwd = Path(cwd).resolve() if cwd else Path.cwd()
    spec_path = Path(tempfile.gettempdir()) / f"agentcad-scene-{uuid.uuid4().hex}.json"
    png_path = Path(tempfile.gettempdir()) / f"agentcad-scene-{uuid.uuid4().hex}.png"
    json_path = Path(tempfile.gettempdir()) / f"agentcad-scene-{uuid.uuid4().hex}.json"

    if sketches:
        sketches_manifest_path = (
            Path(tempfile.gettempdir())
            / f"agentcad-sketches-{uuid.uuid4().hex}.json"
        )
        sketches_manifest_path.write_text(json.dumps([
            {
                "name": s["name"],
                "script": str(Path(s["script"]).resolve()),
                "params": str(Path(s["params"]).resolve()),
            }
            for s in sketches
        ]), encoding="utf-8")
        spec = dict(spec)
        spec["sketches_manifest"] = str(sketches_manifest_path)

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

    if not result.get("ok"):
        if png_path.exists():
            try:
                png_path.unlink()
            except OSError:
                pass
        return SnapshotResult(ok=False, error=result.get("error") or "scene render failed",
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


def snapshot(script_path: Path, params_path: Path, view: dict, *,
             cwd: Path | None = None, width: int = 800, height: int = 600,
             timeout: float = 30.0,
             sketches: list[dict] | None = None) -> SnapshotResult:
    """Render a PNG snapshot of the given script via the snapshot worker subprocess.

    `sketches`: optional manifest entries — same shape as `run()`. The
    snapshot worker injects them as a `sketches` dict so an object script
    that consumes sketches renders correctly here too.
    """
    script_path = Path(script_path).resolve()
    params_path = Path(params_path).resolve()
    cwd = Path(cwd).resolve() if cwd else script_path.parent
    png_path = Path(tempfile.gettempdir()) / f"agentcad-snap-{uuid.uuid4().hex}.png"
    json_path = Path(tempfile.gettempdir()) / f"agentcad-snap-{uuid.uuid4().hex}.json"
    sketches_arg = "-"
    if sketches:
        sketches_path = (
            Path(tempfile.gettempdir())
            / f"agentcad-sketches-{uuid.uuid4().hex}.json"
        )
        sketches_path.write_text(json.dumps([
            {
                "name": s["name"],
                "script": str(Path(s["script"]).resolve()),
                "params": str(Path(s["params"]).resolve()),
            }
            for s in sketches
        ]), encoding="utf-8")
        sketches_arg = str(sketches_path)

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
                sketches_arg,
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
