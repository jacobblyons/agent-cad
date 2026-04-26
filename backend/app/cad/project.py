"""An Agent CAD project — a directory on disk that's also a git repo.

A project holds *objects* and *sketches*. An object is a CADQuery script
that produces a Workplane (the actual 3D geometry); a sketch is a
CADQuery script that produces a 2D profile (cq.Sketch) sitting on a
named workplane in 3D space. Object scripts may consume sketches by
name through an injected `sketches` dict; that lets the agent do the
fully-constrained-sketch-first workflow Fusion users expect.

The user (and agent) work on one *active artifact* at a time — either
an object or a sketch. The viewer renders all visible objects as 3D
geometry and all visible sketches as line overlays.

Layout (current):
    <project>/
      .git/
      objects/
        <name>.py            # CADQuery script defining `model`
        <name>.params.json
      sketches/
        <name>.py            # CADQuery script defining `sketch` (+ optional `plane`)
        <name>.params.json
      state.json             # {"active_kind": "object"|"sketch", "active_object": ..., "active_sketch": ...}
      chat.jsonl
      assets/

Legacy single-object layout (still supported on disk, auto-migrates the
first time a 2nd object or any sketch is added):
    <project>/
      .git/
      model.py
      params.json
      chat.jsonl
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

WORKSPACE_ROOT = Path.home() / ".agent-cad" / "projects"

_NAME_BAD = re.compile(r"[^A-Za-z0-9._\- ]+")
_OBJ_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")
_OBJ_NAME_BAD = re.compile(r"[^A-Za-z0-9_\-]+")

DEFAULT_OBJECT_NAME = "main"


def sanitize_name(name: str) -> str:
    """Project directory name: collapse weird chars to '-', trim junk."""
    name = (name or "").strip()
    name = _NAME_BAD.sub("-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip(" .-")


def sanitize_object_name(name: str) -> str:
    """Object filename: stricter — alphanumeric, underscore, dash; must start with letter."""
    name = (name or "").strip()
    name = _OBJ_NAME_BAD.sub("-", name)
    name = re.sub(r"-+", "-", name).strip("-_")
    if not name:
        return ""
    if not name[0].isalpha():
        name = "obj-" + name
    return name


def list_recent(parent_dir: Path) -> list[dict]:
    """Scan a directory for Agent CAD projects (subdirs containing model.py
    or objects/<x>.py)."""
    parent_dir = Path(parent_dir).expanduser()
    if not parent_dir.exists():
        return []
    out: list[dict] = []
    for child in parent_dir.iterdir():
        if not child.is_dir():
            continue
        has_old = (child / "model.py").exists()
        has_new = (child / "objects").is_dir() and any(
            (child / "objects").glob("*.py")
        )
        if not (has_old or has_new):
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        head_subject = ""
        head_sha = ""
        if (child / ".git").exists():
            try:
                r = subprocess.run(
                    ["git", "log", "-1", "--pretty=%H%x1f%s"],
                    cwd=str(child), capture_output=True, text=True, check=False,
                )
                if r.returncode == 0 and r.stdout.strip():
                    parts = r.stdout.strip().split("\x1f", 1)
                    head_sha = parts[0]
                    if len(parts) > 1:
                        head_subject = parts[1]
            except OSError:
                pass
        out.append({
            "path": str(child),
            "title": child.name,
            "head_sha": head_sha,
            "head_subject": head_subject,
            "modified": mtime,
        })
    out.sort(key=lambda r: r["modified"], reverse=True)
    return out


SEED_MODEL = '''"""Agent CAD object — edit this file (or let the agent edit it).

Conventions:
- Define a top-level `model` (a cadquery.Workplane).
- Read tweakable values from `params` (a dict) — the runtime injects this
  from the object's params.json so the user can adjust without re-committing.
- Sketches defined in this project are injected as a `sketches` dict
  (name → cq.Workplane already placed on the sketch's plane). When a
  feature naturally maps to a 2D profile, prefer building the profile as
  a sketch and extruding/lofting/sweeping from `sketches["name"]` rather
  than inlining the geometry here.
- Tag faces/sketches you intend to reference later, e.g. .tag("top").
"""
import cadquery as cq

length = float(params.get("length", 30))
width  = float(params.get("width",  30))
height = float(params.get("height", 15))

model = cq.Workplane("XY").rect(length, width).extrude(height)
'''
SEED_PARAMS = {"length": 30, "width": 30, "height": 15}

SEED_SKETCH = '''"""Agent CAD sketch — a fully-constrained 2D profile.

Conventions:
- Define a top-level `sketch` (a cadquery.Sketch). Build it with explicit
  numeric dimensions or named params — never implicit defaults.
- Optionally define `plane` to control where the sketch sits in 3D:
    plane = "XY"               # default
    plane = "XZ"               # vertical, normal +Y
    plane = ("XY", 5.0)        # XY offset 5mm along +Z
    plane = cq.Plane(...)      # full custom plane
  If omitted, the sketch sits on XY at the origin.
- Read tweakable values from `params` (a dict). Each sketch has its own
  params namespace.
- Use .constrain(...).solve() if you need explicit geometric constraints
  beyond what .rect / .circle / .polyline already pin down.

Object scripts can consume this sketch through an injected `sketches`
dict — `sketches["this-name"]` is a cq.Workplane already placed on the
sketch's plane, ready to .extrude() / .loft() / .sweep().
"""
import cadquery as cq

length = float(params.get("length", 30))
width  = float(params.get("width",  20))

sketch = cq.Sketch().rect(length, width)
plane = "XY"
'''
SEED_SKETCH_PARAMS = {"length": 30, "width": 20}


@dataclass
class GitCommit:
    sha: str
    short: str
    subject: str
    body: str
    author: str
    date_iso: str

    def to_json(self) -> dict:
        return {
            "sha": self.sha,
            "short": self.short,
            "subject": self.subject,
            "body": self.body,
            "author": self.author,
            "date": self.date_iso,
        }


class Project:
    """A project on disk. All paths are absolute."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    # --- factories -----------------------------------------------------

    @classmethod
    def create_named(cls, parent_dir: Path, name: str) -> "Project":
        """Create a project at <parent_dir>/<sanitized name>/."""
        safe = sanitize_name(name)
        if not safe:
            raise ValueError("project name cannot be empty")
        parent = Path(parent_dir).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / safe
        if target.exists():
            raise FileExistsError(f"a project named '{safe}' already exists at {target}")
        return cls.init_at(path=target, title=name)

    @classmethod
    def init_at(cls, path: Path | None = None, *, title: str | None = None) -> "Project":
        """Create a new empty project (multi-object layout from the start)."""
        if path is None:
            WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
            path = WORKSPACE_ROOT / f"untitled-{uuid.uuid4().hex[:8]}"
        path = Path(path).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        (path / "assets").mkdir(exist_ok=True)
        objects_dir = path / "objects"
        objects_dir.mkdir(exist_ok=True)
        (path / "sketches").mkdir(exist_ok=True)
        (objects_dir / f"{DEFAULT_OBJECT_NAME}.py").write_text(SEED_MODEL, encoding="utf-8")
        (objects_dir / f"{DEFAULT_OBJECT_NAME}.params.json").write_text(
            json.dumps(SEED_PARAMS, indent=2), encoding="utf-8",
        )
        (path / "state.json").write_text(
            json.dumps({
                "active_kind": "object",
                "active_object": DEFAULT_OBJECT_NAME,
                "active_sketch": None,
            }, indent=2), encoding="utf-8",
        )
        (path / "chat.jsonl").write_text("", encoding="utf-8")
        (path / ".gitignore").write_text("__pycache__/\n*.pyc\n.agentcad-cache/\n", encoding="utf-8")
        proj = cls(path)
        proj._git("init", "-q", "-b", "main")
        if not proj._git_safe("config", "user.email").strip():
            proj._git("config", "user.email", "agent-cad@localhost")
            proj._git("config", "user.name", "agent-cad")
        proj._git("add", "-A")
        proj._git("commit", "-q", "-m", title or "initial project")
        return proj

    @classmethod
    def open(cls, path: Path) -> "Project":
        proj = cls(path)
        if not proj._has_any_object_on_disk():
            raise FileNotFoundError(f"no model.py or objects/ in {path}")
        if not (proj.path / ".git").exists():
            proj._git("init", "-q", "-b", "main")
            proj._git("add", "-A")
            proj._git("commit", "-q", "-m", "adopt existing files")
        return proj

    # --- paths ---------------------------------------------------------

    @property
    def objects_dir(self) -> Path:
        return self.path / "objects"

    @property
    def sketches_dir(self) -> Path:
        return self.path / "sketches"

    @property
    def imports_dir(self) -> Path:
        return self.path / "imports"

    @property
    def state_path(self) -> Path:
        return self.path / "state.json"

    @property
    def chat_path(self) -> Path:
        return self.path / "chat.jsonl"

    @property
    def title(self) -> str:
        return self.path.name

    @property
    def id(self) -> str:
        return str(self.path)

    # Legacy single-file paths, used only when objects/ doesn't exist.
    @property
    def _legacy_model_path(self) -> Path:
        return self.path / "model.py"

    @property
    def _legacy_params_path(self) -> Path:
        return self.path / "params.json"

    # --- objects -------------------------------------------------------

    def _is_multi_layout(self) -> bool:
        return self.objects_dir.is_dir()

    def _has_any_object_on_disk(self) -> bool:
        if self._is_multi_layout() and any(self.objects_dir.glob("*.py")):
            return True
        return self._legacy_model_path.exists()

    def list_objects(self) -> list[dict]:
        """Return all objects in this project, ordered alphabetically."""
        objs: list[dict] = []
        if self._is_multi_layout():
            for src in sorted(self.objects_dir.glob("*.py")):
                name = src.stem
                objs.append(self._object_meta(name))
        elif self._legacy_model_path.exists():
            objs.append(self._object_meta(DEFAULT_OBJECT_NAME))
        return objs

    def _object_meta(self, name: str) -> dict:
        src = self.object_source_path(name)
        try:
            mtime = src.stat().st_mtime
        except OSError:
            mtime = 0.0
        return {
            "name": name,
            "modified": mtime,
            "visible": self.is_object_visible(name),
            "requirements": self.list_requirements(name),
        }

    def is_object_visible(self, name: str) -> bool:
        vis = self._read_state().get("visibility") or {}
        # Default visible — only flip if user explicitly hid it.
        return vis.get(name, True)

    def list_requirements(self, name: str) -> list[str]:
        """Ordered list of user-defined requirements for this object."""
        reqs = self._read_state().get("requirements") or {}
        items = reqs.get(name) or []
        return [str(r) for r in items if str(r).strip()]

    def set_requirements(self, name: str, requirements: list[str]) -> None:
        if not self.object_exists(name):
            raise FileNotFoundError(f"object '{name}' does not exist")
        cleaned = [str(r).strip() for r in requirements if str(r).strip()]
        state = self._read_state()
        reqs = dict(state.get("requirements") or {})
        if cleaned:
            reqs[name] = cleaned
        else:
            reqs.pop(name, None)
        state["requirements"] = reqs
        self._write_state(state)

    def set_object_visible(self, name: str, visible: bool) -> None:
        if not self.object_exists(name):
            raise FileNotFoundError(f"object '{name}' does not exist")
        state = self._read_state()
        vis = dict(state.get("visibility") or {})
        if visible:
            vis.pop(name, None)  # default is visible; no need to store
        else:
            vis[name] = False
        state["visibility"] = vis
        self._write_state(state)

    def object_exists(self, name: str) -> bool:
        return self.object_source_path(name, _check=False).exists()

    def object_source_path(self, name: str, *, _check: bool = True) -> Path:
        """Return the path to the object's CADQuery script."""
        if self._is_multi_layout():
            return self.objects_dir / f"{name}.py"
        if name == DEFAULT_OBJECT_NAME:
            return self._legacy_model_path
        # In legacy layout, only DEFAULT_OBJECT_NAME exists. Return a path
        # that doesn't exist so callers can detect missing objects.
        return self.objects_dir / f"{name}.py"

    def object_params_path(self, name: str) -> Path:
        if self._is_multi_layout():
            return self.objects_dir / f"{name}.params.json"
        if name == DEFAULT_OBJECT_NAME:
            return self._legacy_params_path
        return self.objects_dir / f"{name}.params.json"

    def read_object_source(self, name: str) -> str:
        return self.object_source_path(name).read_text(encoding="utf-8")

    def write_object_source(self, name: str, src: str) -> None:
        path = self.object_source_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")

    def read_object_params(self, name: str) -> dict:
        path = self.object_params_path(name)
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            return {}

    def write_object_params(self, name: str, params: dict) -> None:
        path = self.object_params_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(params, indent=2), encoding="utf-8")

    def _ensure_multi_layout(self) -> None:
        """Migrate a legacy single-file project to the objects/ layout
        (in the working tree only — the next commit will record it)."""
        if self._is_multi_layout():
            return
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        if self._legacy_model_path.exists():
            shutil.move(
                str(self._legacy_model_path),
                str(self.objects_dir / f"{DEFAULT_OBJECT_NAME}.py"),
            )
        if self._legacy_params_path.exists():
            shutil.move(
                str(self._legacy_params_path),
                str(self.objects_dir / f"{DEFAULT_OBJECT_NAME}.params.json"),
            )

    def create_object(self, name: str) -> str:
        """Create a new object with the seed script. Returns the safe name."""
        safe = sanitize_object_name(name)
        if not safe:
            raise ValueError("object name cannot be empty")
        self._ensure_multi_layout()
        target = self.object_source_path(safe)
        if target.exists():
            raise FileExistsError(f"object '{safe}' already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SEED_MODEL, encoding="utf-8")
        self.object_params_path(safe).write_text(
            json.dumps(SEED_PARAMS, indent=2), encoding="utf-8",
        )
        self.set_active_object(safe)
        return safe

    def rename_object(self, old: str, new: str) -> str:
        safe = sanitize_object_name(new)
        if not safe:
            raise ValueError("object name cannot be empty")
        if safe == old:
            return safe
        self._ensure_multi_layout()
        src = self.object_source_path(old)
        if not src.exists():
            raise FileNotFoundError(f"object '{old}' does not exist")
        dst = self.object_source_path(safe)
        if dst.exists():
            raise FileExistsError(f"object '{safe}' already exists")
        shutil.move(str(src), str(dst))
        old_params = self.object_params_path(old)
        if old_params.exists():
            shutil.move(str(old_params), str(self.object_params_path(safe)))
        # Carry visibility + requirements forward under the new name.
        state = self._read_state()
        dirty = False
        vis = dict(state.get("visibility") or {})
        if old in vis:
            vis[safe] = vis.pop(old)
            state["visibility"] = vis
            dirty = True
        reqs = dict(state.get("requirements") or {})
        if old in reqs:
            reqs[safe] = reqs.pop(old)
            state["requirements"] = reqs
            dirty = True
        if dirty:
            self._write_state(state)
        if self.active_object() == old:
            self.set_active_object(safe)
        return safe

    def delete_object(self, name: str) -> None:
        objs = [o["name"] for o in self.list_objects()]
        if name not in objs:
            raise FileNotFoundError(f"object '{name}' does not exist")
        if len(objs) <= 1:
            raise ValueError("cannot delete the last remaining object")
        self._ensure_multi_layout()
        src = self.object_source_path(name)
        if src.exists():
            src.unlink()
        params = self.object_params_path(name)
        if params.exists():
            params.unlink()
        # Drop any visibility / requirements entries for the deleted object.
        state = self._read_state()
        dirty = False
        vis = dict(state.get("visibility") or {})
        if name in vis:
            vis.pop(name)
            state["visibility"] = vis
            dirty = True
        reqs = dict(state.get("requirements") or {})
        if name in reqs:
            reqs.pop(name)
            state["requirements"] = reqs
            dirty = True
        if dirty:
            self._write_state(state)
        if self.active_object() == name:
            remaining = [o for o in objs if o != name]
            self.set_active_object(remaining[0])

    # --- sketches -----------------------------------------------------

    def _ensure_sketches_dir(self) -> None:
        self.sketches_dir.mkdir(parents=True, exist_ok=True)

    def list_sketches(self) -> list[dict]:
        """All sketches in this project, ordered alphabetically."""
        if not self.sketches_dir.is_dir():
            return []
        return [self._sketch_meta(p.stem) for p in sorted(self.sketches_dir.glob("*.py"))]

    def _sketch_meta(self, name: str) -> dict:
        src = self.sketch_source_path(name)
        try:
            mtime = src.stat().st_mtime
        except OSError:
            mtime = 0.0
        return {
            "name": name,
            "modified": mtime,
            "visible": self.is_sketch_visible(name),
        }

    def sketch_exists(self, name: str) -> bool:
        return self.sketch_source_path(name).exists()

    def sketch_source_path(self, name: str) -> Path:
        return self.sketches_dir / f"{name}.py"

    def sketch_params_path(self, name: str) -> Path:
        return self.sketches_dir / f"{name}.params.json"

    def read_sketch_source(self, name: str) -> str:
        return self.sketch_source_path(name).read_text(encoding="utf-8")

    def write_sketch_source(self, name: str, src: str) -> None:
        path = self.sketch_source_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")

    def read_sketch_params(self, name: str) -> dict:
        path = self.sketch_params_path(name)
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except FileNotFoundError:
            return {}

    def write_sketch_params(self, name: str, params: dict) -> None:
        path = self.sketch_params_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(params, indent=2), encoding="utf-8")

    def is_sketch_visible(self, name: str) -> bool:
        # Default: visible. The state.json key for sketches is separate from
        # objects so the two visibility namespaces don't collide on identical
        # names.
        vis = self._read_state().get("sketch_visibility") or {}
        return vis.get(name, True)

    def set_sketch_visible(self, name: str, visible: bool) -> None:
        if not self.sketch_exists(name):
            raise FileNotFoundError(f"sketch '{name}' does not exist")
        state = self._read_state()
        vis = dict(state.get("sketch_visibility") or {})
        if visible:
            vis.pop(name, None)
        else:
            vis[name] = False
        state["sketch_visibility"] = vis
        self._write_state(state)

    def create_sketch(self, name: str) -> str:
        """Create a new sketch with the seed script. Returns the safe name."""
        safe = sanitize_object_name(name)
        if not safe:
            raise ValueError("sketch name cannot be empty")
        self._ensure_sketches_dir()
        target = self.sketch_source_path(safe)
        if target.exists():
            raise FileExistsError(f"sketch '{safe}' already exists")
        target.write_text(SEED_SKETCH, encoding="utf-8")
        self.sketch_params_path(safe).write_text(
            json.dumps(SEED_SKETCH_PARAMS, indent=2), encoding="utf-8",
        )
        self.set_active_sketch(safe)
        return safe

    def rename_sketch(self, old: str, new: str) -> str:
        safe = sanitize_object_name(new)
        if not safe:
            raise ValueError("sketch name cannot be empty")
        if safe == old:
            return safe
        src = self.sketch_source_path(old)
        if not src.exists():
            raise FileNotFoundError(f"sketch '{old}' does not exist")
        dst = self.sketch_source_path(safe)
        if dst.exists():
            raise FileExistsError(f"sketch '{safe}' already exists")
        shutil.move(str(src), str(dst))
        old_params = self.sketch_params_path(old)
        if old_params.exists():
            shutil.move(str(old_params), str(self.sketch_params_path(safe)))
        state = self._read_state()
        dirty = False
        vis = dict(state.get("sketch_visibility") or {})
        if old in vis:
            vis[safe] = vis.pop(old)
            state["sketch_visibility"] = vis
            dirty = True
        if state.get("active_sketch") == old:
            state["active_sketch"] = safe
            dirty = True
        if dirty:
            self._write_state(state)
        return safe

    def delete_sketch(self, name: str) -> None:
        if not self.sketch_exists(name):
            raise FileNotFoundError(f"sketch '{name}' does not exist")
        self.sketch_source_path(name).unlink()
        params = self.sketch_params_path(name)
        if params.exists():
            params.unlink()
        state = self._read_state()
        dirty = False
        vis = dict(state.get("sketch_visibility") or {})
        if name in vis:
            vis.pop(name)
            state["sketch_visibility"] = vis
            dirty = True
        # Stepping off a deleted sketch: clear the pointer and flip back to
        # the active object so the agent has somewhere to edit.
        if state.get("active_sketch") == name:
            state["active_sketch"] = None
            if state.get("active_kind") == "sketch":
                state["active_kind"] = "object"
            dirty = True
        if dirty:
            self._write_state(state)

    # --- imports ------------------------------------------------------
    #
    # Imports are user-supplied reference models (STEP only for now). They
    # never become the active artifact — they're not editable, just there
    # for the agent to measure off and boolean against. Stored verbatim
    # under imports/<name>.step; the agent gets them as an `imports` dict
    # of cq.Workplanes when running an object script.

    # B-rep formats give the agent full boolean + measurement support;
    # STL is mesh-only (display + bbox work, booleans don't). Keep this
    # in sync with _import_loader.SUPPORTED_EXTS.
    SUPPORTED_IMPORT_EXTS = {
        ".step", ".stp",
        ".iges", ".igs",
        ".brep", ".brp",
        ".stl",
        ".glb", ".gltf",
        ".3mf",
    }

    def _ensure_imports_dir(self) -> None:
        self.imports_dir.mkdir(parents=True, exist_ok=True)

    def list_imports(self) -> list[dict]:
        """Every import in the project, ordered alphabetically by name."""
        if not self.imports_dir.is_dir():
            return []
        out: list[dict] = []
        for p in sorted(self.imports_dir.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in self.SUPPORTED_IMPORT_EXTS:
                continue
            out.append(self._import_meta(p))
        return out

    def _import_meta(self, source_path: Path) -> dict:
        try:
            mtime = source_path.stat().st_mtime
            size = source_path.stat().st_size
        except OSError:
            mtime = 0.0
            size = 0
        return {
            "name": source_path.stem,
            "ext": source_path.suffix.lstrip(".").lower(),
            "modified": mtime,
            "size_bytes": size,
            "visible": self.is_import_visible(source_path.stem),
        }

    def import_exists(self, name: str) -> bool:
        return self.import_source_path(name) is not None

    def import_source_path(self, name: str) -> Path | None:
        """First file matching a supported extension. None if not present."""
        if not self.imports_dir.is_dir():
            return None
        for ext in self.SUPPORTED_IMPORT_EXTS:
            cand = self.imports_dir / f"{name}{ext}"
            if cand.exists():
                return cand
        return None

    def is_import_visible(self, name: str) -> bool:
        vis = self._read_state().get("import_visibility") or {}
        return vis.get(name, True)

    def set_import_visible(self, name: str, visible: bool) -> None:
        if not self.import_exists(name):
            raise FileNotFoundError(f"import '{name}' does not exist")
        state = self._read_state()
        vis = dict(state.get("import_visibility") or {})
        if visible:
            vis.pop(name, None)
        else:
            vis[name] = False
        state["import_visibility"] = vis
        self._write_state(state)

    def create_import(self, source_file: Path, name: str | None = None) -> str:
        """Copy an external STEP file into imports/<name>.<ext>.

        `name` defaults to the file's stem (sanitized). If a sketch / object
        with that name already exists in their respective namespace it's
        fine — the import namespace is independent.
        """
        source_file = Path(source_file).expanduser().resolve()
        if not source_file.exists():
            raise FileNotFoundError(f"file not found: {source_file}")
        ext = source_file.suffix.lower()
        if ext not in self.SUPPORTED_IMPORT_EXTS:
            raise ValueError(
                f"unsupported import format {ext!r}; expected one of "
                f"{sorted(self.SUPPORTED_IMPORT_EXTS)}"
            )
        safe = sanitize_object_name(name or source_file.stem)
        if not safe:
            raise ValueError("import name cannot be empty")
        self._ensure_imports_dir()
        # Refuse to clobber an existing import with the same name (any ext).
        if self.import_exists(safe):
            raise FileExistsError(f"import '{safe}' already exists")
        target = self.imports_dir / f"{safe}{ext}"
        shutil.copyfile(str(source_file), str(target))
        return safe

    def rename_import(self, old: str, new: str) -> str:
        safe = sanitize_object_name(new)
        if not safe:
            raise ValueError("import name cannot be empty")
        if safe == old:
            return safe
        src = self.import_source_path(old)
        if src is None:
            raise FileNotFoundError(f"import '{old}' does not exist")
        if self.import_exists(safe):
            raise FileExistsError(f"import '{safe}' already exists")
        dst = self.imports_dir / f"{safe}{src.suffix}"
        shutil.move(str(src), str(dst))
        state = self._read_state()
        vis = dict(state.get("import_visibility") or {})
        if old in vis:
            vis[safe] = vis.pop(old)
            state["import_visibility"] = vis
            self._write_state(state)
        return safe

    def delete_import(self, name: str) -> None:
        src = self.import_source_path(name)
        if src is None:
            raise FileNotFoundError(f"import '{name}' does not exist")
        src.unlink()
        state = self._read_state()
        vis = dict(state.get("import_visibility") or {})
        if name in vis:
            vis.pop(name)
            state["import_visibility"] = vis
            self._write_state(state)

    # --- active-artifact pointer --------------------------------------

    def _read_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}

    def _write_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def active_object(self) -> str:
        """Name of the currently-active object. Falls back to the first
        available object if state.json is missing or stale (e.g. after a
        checkout that removed the previously-active object)."""
        objs = [o["name"] for o in self.list_objects()]
        if not objs:
            return DEFAULT_OBJECT_NAME
        wanted = self._read_state().get("active_object")
        if wanted in objs:
            return wanted
        return objs[0]

    def set_active_object(self, name: str) -> None:
        if not self.object_exists(name):
            raise FileNotFoundError(f"object '{name}' does not exist")
        state = self._read_state()
        state["active_object"] = name
        # Selecting an object always flips the edit target to "object".
        state["active_kind"] = "object"
        self._write_state(state)

    def active_sketch(self) -> str | None:
        """Name of the currently-selected sketch, or None if no sketch is
        selected (or the previously-selected one was deleted)."""
        sketches = [s["name"] for s in self.list_sketches()]
        if not sketches:
            return None
        wanted = self._read_state().get("active_sketch")
        return wanted if wanted in sketches else None

    def set_active_sketch(self, name: str) -> None:
        if not self.sketch_exists(name):
            raise FileNotFoundError(f"sketch '{name}' does not exist")
        state = self._read_state()
        state["active_sketch"] = name
        state["active_kind"] = "sketch"
        self._write_state(state)

    def active_kind(self) -> str:
        """Whether the agent's edit target is the active object or the active
        sketch. Defaults to 'object'. Falls back to 'object' if 'sketch' is
        selected but no sketch exists."""
        kind = self._read_state().get("active_kind") or "object"
        if kind == "sketch" and self.active_sketch() is None:
            return "object"
        return kind

    def active_artifact(self) -> tuple[str, str]:
        """(kind, name) for whichever artifact is the current edit target.

        Useful for the agent's edit/run tools: they all dispatch through this
        single pointer rather than caring about objects vs. sketches
        individually.
        """
        kind = self.active_kind()
        if kind == "sketch":
            name = self.active_sketch()
            if name is not None:
                return ("sketch", name)
        return ("object", self.active_object())

    def active_script_path(self) -> Path:
        """Path the agent should Read/Edit/Write by default — follows the
        active artifact."""
        kind, name = self.active_artifact()
        return (self.sketch_source_path(name) if kind == "sketch"
                else self.object_source_path(name))

    def active_script_params_path(self) -> Path:
        kind, name = self.active_artifact()
        return (self.sketch_params_path(name) if kind == "sketch"
                else self.object_params_path(name))

    # Convenience for code that wants the active object's paths directly.
    @property
    def active_model_path(self) -> Path:
        return self.object_source_path(self.active_object())

    @property
    def active_params_path(self) -> Path:
        return self.object_params_path(self.active_object())

    # --- legacy aliases (kept for callers that haven't been updated yet) -

    def read_model(self) -> str:
        return self.read_object_source(self.active_object())

    def write_model(self, src: str) -> None:
        self.write_object_source(self.active_object(), src)

    def read_params(self) -> dict:
        return self.read_object_params(self.active_object())

    def write_params(self, params: dict) -> None:
        self.write_object_params(self.active_object(), params)

    # --- chat ----------------------------------------------------------

    def append_chat(self, message: dict) -> None:
        with self.chat_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message, ensure_ascii=False) + "\n")

    def read_chat(self) -> list[dict]:
        if not self.chat_path.exists():
            return []
        return [
            json.loads(l)
            for l in self.chat_path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]

    # --- git -----------------------------------------------------------

    def _git(self, *args: str) -> str:
        out = subprocess.run(
            ["git", *args], cwd=str(self.path),
            capture_output=True, text=True, check=True,
        )
        return out.stdout

    def _git_safe(self, *args: str) -> str:
        out = subprocess.run(
            ["git", *args], cwd=str(self.path),
            capture_output=True, text=True,
        )
        return out.stdout

    def has_uncommitted(self) -> bool:
        return bool(self._git_safe("status", "--porcelain").strip())

    def head_sha(self) -> str:
        return self._git_safe("rev-parse", "HEAD").strip()

    def head_branch(self) -> str:
        return self._git_safe("rev-parse", "--abbrev-ref", "HEAD").strip()

    def commit(self, subject: str, body: str = "") -> str:
        msg = subject + (("\n\n" + body) if body else "")
        self._git("add", "-A")
        try:
            self._git("commit", "-q", "-m", msg)
        except subprocess.CalledProcessError:
            return self.head_sha()
        return self.head_sha()

    def log(self, limit: int = 50) -> list[GitCommit]:
        REC = "\x1e"
        FLD = "\x1f"
        fmt = FLD.join(["%H", "%h", "%s", "%b", "%an", "%aI"]) + REC
        out = self._git_safe(
            "log", f"--pretty=format:{fmt}", "-n", str(limit),
            "--all", "--topo-order",
        )
        commits: list[GitCommit] = []
        for raw in out.split(REC):
            raw = raw.strip("\n")
            if not raw:
                continue
            parts = raw.split(FLD)
            if len(parts) < 6:
                continue
            commits.append(GitCommit(
                sha=parts[0], short=parts[1], subject=parts[2],
                body=parts[3], author=parts[4], date_iso=parts[5],
            ))
        return commits

    def checkout(self, ref: str) -> None:
        self._git("checkout", "-q", "--detach", ref)

    def branch_at(self, ref: str, name: str | None = None) -> str:
        name = name or f"edit-{uuid.uuid4().hex[:6]}"
        self._git("checkout", "-q", "-b", name, ref)
        return name

    # --- export --------------------------------------------------------

    def export_zip(self, dest: Path) -> Path:
        dest = Path(dest)
        if dest.suffix.lower() != ".zip":
            dest = dest.with_suffix(".zip")
        base = dest.with_suffix("")
        result = shutil.make_archive(str(base), "zip", root_dir=str(self.path))
        return Path(result)

    # --- summary -------------------------------------------------------

    def to_json(self) -> dict:
        commits = self.log(limit=200)
        active_obj = self.active_object()
        active_skt = self.active_sketch()
        kind = self.active_kind()
        objects = self.list_objects()
        sketches = self.list_sketches()
        imports = self.list_imports()
        # Surface the active artifact's own params (not merged) — the Tweaks
        # panel edits the file the user / agent is currently working on.
        if kind == "sketch" and active_skt is not None:
            params = self.read_sketch_params(active_skt)
        else:
            params = self.read_object_params(active_obj)
        return {
            "id": self.id,
            "path": str(self.path),
            "title": self.title,
            "head_sha": self.head_sha(),
            "head_branch": self.head_branch(),
            "uncommitted": self.has_uncommitted(),
            "commits": [c.to_json() for c in commits],
            "objects": objects,
            "sketches": sketches,
            "imports": imports,
            "active_kind": kind,
            "active_object": active_obj,
            "active_sketch": active_skt,
            "params": params,
        }
