"""JsApi — Python surface exposed to the webview.

Each public method becomes window.pywebview.api.<name> on the JS side.
Long work pushes events to events.bus instead of blocking the call.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

import webview

from . import __version__, settings
from .cad.project import Project, list_recent
from .cad.script_runner import (
    RunResult,
    run as run_script,
    tessellate_sketch as tessellate_sketch_script,
)
from .events import bus


def _sketches_manifest(project: Project) -> list[dict]:
    """Manifest entries for every sketch in this project — handed to the
    script / snapshot / scene runners so object scripts can consume them."""
    return [
        {
            "name": s["name"],
            "script": str(project.sketch_source_path(s["name"])),
            "params": str(project.sketch_params_path(s["name"])),
        }
        for s in project.list_sketches()
    ]


class JsApi:
    def __init__(self) -> None:
        self._t0 = time.time()
        self._projects: dict[str, Project] = {}

    # --- diagnostics ---------------------------------------------------

    def ping(self) -> dict:
        return {"ok": True, "version": __version__, "uptime_s": time.time() - self._t0}

    # --- settings ------------------------------------------------------

    def settings_get(self) -> dict:
        return {
            "ok": True,
            "settings": settings.load().to_json(),
            "models": settings.KNOWN_MODELS,
            "efforts": settings.KNOWN_EFFORTS,
        }

    def settings_set(self, patch: dict) -> dict:
        try:
            s = settings.update(**(patch or {}))
            return {"ok": True, "settings": s.to_json()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def pick_directory(self) -> dict:
        """Open a folder picker; return the chosen path. No-op cancel
        returns ok=False, cancelled=True."""
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "no window"}
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"ok": False, "cancelled": True}
        return {"ok": True, "path": result[0]}

    # --- projects ------------------------------------------------------

    def project_create(self, name: str) -> dict:
        s = settings.load()
        try:
            project = Project.create_named(Path(s.default_project_dir), name)
        except (FileExistsError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e), "trace": traceback.format_exc()}
        self._projects[project.id] = project
        self._emit_all_visible_geometry(project)
        return {"ok": True, "project": project.to_json()}

    def project_open(self, path: str) -> dict:
        try:
            project = Project.open(Path(path))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._projects[project.id] = project
        self._emit_all_visible_geometry(project)
        return {"ok": True, "project": project.to_json()}

    def project_pick_external(self) -> dict:
        """Open a folder picker, then load the chosen folder as a project."""
        picked = self.pick_directory()
        if not picked.get("ok"):
            return picked
        return self.project_open(picked["path"])

    def project_list_recent(self) -> dict:
        s = settings.load()
        return {
            "ok": True,
            "default_dir": s.default_project_dir,
            "projects": list_recent(Path(s.default_project_dir)),
        }

    def project_state(self, project_id: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        return {"ok": True, "project": proj.to_json()}

    def project_close(self, project_id: str) -> dict:
        """Release the in-memory Project for a closed tab. Files on disk
        are untouched."""
        self._projects.pop(project_id, None)
        return {"ok": True}

    def project_read_model(self, project_id: str, object_name: str | None = None) -> dict:
        """Read the source of an object (defaults to the active one)."""
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        name = object_name or proj.active_object()
        try:
            return {"ok": True, "name": name, "source": proj.read_object_source(name)}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}

    def project_write_model(self, project_id: str, source: str,
                            object_name: str | None = None) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        name = object_name or proj.active_object()
        try:
            proj.write_object_source(name, source)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        self._emit_object_geometry(proj, name)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True}

    def project_set_parameter(self, project_id: str, name: str, value: float,
                              object_name: str | None = None) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        # Tweaks-panel writes follow the active artifact (object or sketch).
        # `object_name` is preserved for explicit overrides — if set, target
        # that object regardless of the active kind.
        if object_name:
            kind = "object"
            target = object_name
        else:
            kind, target = proj.active_artifact()
        if kind == "sketch":
            params = proj.read_sketch_params(target)
            params[name] = float(value)
            proj.write_sketch_params(target, params)
            self._emit_sketch_geometry(proj, target)
            # Sketch param changes propagate into any object that consumes it.
            for o in proj.list_objects():
                if o.get("visible", True):
                    self._emit_object_geometry(proj, o["name"])
        else:
            params = proj.read_object_params(target)
            params[name] = float(value)
            proj.write_object_params(target, params)
            self._emit_object_geometry(proj, target)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "params": params}

    def project_refresh(self, project_id: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        self._emit_all_visible_geometry(proj)
        return {"ok": True}

    def project_commit(self, project_id: str, subject: str = "save") -> dict:
        """Commit the working tree as a checkpoint on the timeline."""
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        if not proj.has_uncommitted():
            bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
            return {"ok": True, "noop": True}
        try:
            sha = proj.commit(subject or "save")
        except Exception as e:
            return {"ok": False, "error": str(e)}
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "sha": sha}

    def project_export_zip(self, project_id: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "no window"}
        result = window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename=f"{proj.title}.zip",
            file_types=("Zip archive (*.zip)",),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        path = result if isinstance(result, str) else result[0]
        out = proj.export_zip(path)
        return {"ok": True, "path": str(out)}

    # --- objects -------------------------------------------------------

    def object_create(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            safe = proj.create_object(name)
        except (ValueError, FileExistsError) as e:
            return {"ok": False, "error": str(e)}
        self._emit_object_geometry(proj, safe)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "name": safe, "project": proj.to_json()}

    def object_rename(self, project_id: str, old: str, new: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            safe = proj.rename_object(old, new)
        except (ValueError, FileExistsError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
        # The viewer's GLB cache is keyed by name — re-emit so the new key exists.
        if safe != old:
            self._emit_object_geometry(proj, safe)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "name": safe, "project": proj.to_json()}

    def object_delete(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.delete_object(name)
        except (ValueError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    def object_set_active(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.set_active_object(name)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        # No geometry emit — every visible object is already in the viewer.
        # active_object now controls only the params panel and edit target.
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    def object_set_visible(self, project_id: str, name: str, visible: bool) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.set_object_visible(name, bool(visible))
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        # On toggling visible, ensure the GLB is in the viewer's cache.
        if visible:
            self._emit_object_geometry(proj, name)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    def object_set_requirements(self, project_id: str, name: str,
                                requirements: list[str]) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.set_requirements(name, list(requirements or []))
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    # --- sketches ------------------------------------------------------

    def sketch_create(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            safe = proj.create_sketch(name)
        except (ValueError, FileExistsError) as e:
            return {"ok": False, "error": str(e)}
        self._emit_sketch_geometry(proj, safe)
        # New sketch may be referenced by visible objects; re-render them
        # so their `sketches` dict is up to date in the viewer cache.
        for o in proj.list_objects():
            if o.get("visible", True):
                self._emit_object_geometry(proj, o["name"])
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "name": safe, "project": proj.to_json()}

    def sketch_rename(self, project_id: str, old: str, new: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            safe = proj.rename_sketch(old, new)
        except (ValueError, FileExistsError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
        if safe != old:
            self._emit_sketch_geometry(proj, safe)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "name": safe, "project": proj.to_json()}

    def sketch_delete(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.delete_sketch(name)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        # Drop the cached overlay on the frontend.
        bus.emit("doc_sketch_geometry", {
            "doc_id": proj.id, "sketch": name, "ok": False, "deleted": True,
        })
        # Object scripts that referenced this sketch will fail or change shape
        # — re-render visible objects so the viewer reflects the new state.
        for o in proj.list_objects():
            if o.get("visible", True):
                self._emit_object_geometry(proj, o["name"])
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    def sketch_set_active(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.set_active_sketch(name)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    def sketch_set_visible(self, project_id: str, name: str, visible: bool) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.set_sketch_visible(name, bool(visible))
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        if visible:
            self._emit_sketch_geometry(proj, name)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    # --- timeline ------------------------------------------------------

    def timeline_checkout(self, project_id: str, ref: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.checkout(ref)
            self._emit_all_visible_geometry(proj)
            return {"ok": True, "project": proj.to_json()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def timeline_branch(self, project_id: str, ref: str, name: str | None = None) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            branch = proj.branch_at(ref, name)
            self._emit_all_visible_geometry(proj)
            return {"ok": True, "branch": branch, "project": proj.to_json()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # --- chat / agent --------------------------------------------------

    def chat_send(self, project_id: str, text: str,
                  attachments: list[dict] | None = None) -> dict:
        from .agent.runner import run_chat_turn
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        proj.append_chat({"role": "user", "text": text, "attachments": attachments or []})
        run_chat_turn(
            project=proj,
            prompt=text,
            on_run=lambda result: self._emit_run(proj, result),
            attachments=attachments,
        )
        return {"ok": True}

    # --- internals ----------------------------------------------------

    def _emit_object_geometry(self, project: Project, name: str) -> None:
        """Run one object's script and push its geometry to the viewer."""
        try:
            result = run_script(
                project.object_source_path(name),
                project.object_params_path(name),
                cwd=project.path,
                timeout=30.0,
                sketches=_sketches_manifest(project),
            )
        except Exception as e:
            bus.emit("doc_geometry", {
                "doc_id": project.id,
                "object": name,
                "error": f"run failed: {e}",
                "trace": traceback.format_exc(),
            })
            return
        bus.emit("doc_geometry", {
            "doc_id": project.id,
            "object": name,
            "ok": result.ok,
            "error": result.error,
            "stderr": result.stderr,
            "meta": result.meta,
            "glb_b64": result.glb_b64,
            "topology": result.topology,
        })

    def _emit_sketch_geometry(self, project: Project, name: str) -> None:
        """Tessellate one sketch's wires and push to the viewer overlay."""
        try:
            result = tessellate_sketch_script(
                project.sketch_source_path(name),
                project.sketch_params_path(name),
                cwd=project.path,
                timeout=20.0,
            )
        except Exception as e:
            bus.emit("doc_sketch_geometry", {
                "doc_id": project.id,
                "sketch": name,
                "ok": False,
                "error": f"sketch run failed: {e}",
            })
            return
        bus.emit("doc_sketch_geometry", {
            "doc_id": project.id,
            "sketch": name,
            "ok": result.ok,
            "error": result.error,
            "stderr": result.stderr,
            "plane": result.plane,
            "polylines": result.polylines,
            "bbox": result.bbox,
        })

    def _emit_all_visible_geometry(self, project: Project) -> None:
        """Render every visible artifact (objects + sketches) and emit
        per-item geometry events. Sketches are tessellated first so the
        objects that consume them see fresh geometry on their own runs."""
        # Sketches don't depend on each other, so order within the group is
        # arbitrary — but they MUST come before objects that may consume them
        # (the object runner reads each sketch's source on every invocation,
        # so this isn't strictly required for correctness, just for ordering
        # of the events the viewer sees).
        for s in project.list_sketches():
            if s.get("visible", True):
                self._emit_sketch_geometry(project, s["name"])
        for o in project.list_objects():
            if o.get("visible", True):
                self._emit_object_geometry(project, o["name"])
        bus.emit("project_state", {"doc_id": project.id, "state": project.to_json()})

    def _emit_run(self, project: Project, result: RunResult) -> None:
        kind, name = project.active_artifact()
        if kind == "sketch":
            # The agent's run_model on an active sketch surfaces sketch wires
            # to the overlay rather than GLB to the 3D viewer.
            self._emit_sketch_geometry(project, name)
        else:
            bus.emit("doc_geometry", {
                "doc_id": project.id,
                "object": name,
                "ok": result.ok,
                "error": result.error,
                "stderr": result.stderr,
                "meta": result.meta,
                "glb_b64": result.glb_b64,
                "topology": result.topology,
            })
        bus.emit("project_state", {"doc_id": project.id, "state": project.to_json()})
