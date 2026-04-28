"""JsApi — Python surface exposed to the webview.

Each public method becomes window.pywebview.api.<name> on the JS side.
Long work pushes events to events.bus instead of blocking the call.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

import webview

from . import __version__, browser_session, permissions, settings
from .cad.project import Project, list_recent
from .cad.script_runner import (
    RunResult,
    export_models as export_models_script,
    run as run_script,
    tessellate_import as tessellate_import_script,
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


def _imports_manifest(project: Project) -> list[dict]:
    """Manifest entries for every STEP import in this project."""
    out: list[dict] = []
    for i in project.list_imports():
        path = project.import_source_path(i["name"])
        if path is None:
            continue
        out.append({"name": i["name"], "path": str(path)})
    return out


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

    def pick_file(self, file_types: list[str] | None = None) -> dict:
        """Open a file picker; return the chosen file path. `file_types` is a
        list of strings like ['STEP files (*.step;*.stp)']."""
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "no window"}
        types = tuple(file_types) if file_types else ()
        result = window.create_file_dialog(webview.OPEN_DIALOG, file_types=types)
        if not result:
            return {"ok": False, "cancelled": True}
        path = result if isinstance(result, str) else result[0]
        return {"ok": True, "path": path}

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

    def project_export_object(self, project_id: str, name: str) -> dict:
        """Export one object as STL / STEP / BREP. Opens a save dialog;
        format is inferred from the chosen extension."""
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        if not proj.object_exists(name):
            return {"ok": False, "error": f"object {name!r} does not exist"}
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "no window"}
        # Default the save dialog to <project>/exports/ — keeps build
        # artifacts colocated with the project they came from instead of
        # scattering them across whatever directory the user happened to
        # pick last. Created on demand so empty projects don't have a
        # stray empty folder.
        exports_dir = proj.path / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        result = window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(exports_dir),
            save_filename=f"{name}.stl",
            file_types=(
                "STL 3D printing (*.stl)",
                "STEP parametric CAD (*.step)",
                "3MF modern STL replacement (*.3mf)",
                "BREP OpenCascade native (*.brep)",
            ),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        path = Path(result if isinstance(result, str) else result[0])
        items = [{
            "name": name,
            "script": str(proj.object_source_path(name)),
            "params": str(proj.object_params_path(name)),
        }]
        try:
            er = export_models_script(
                items, path, cwd=proj.path,
                sketches=_sketches_manifest(proj),
                imports=_imports_manifest(proj),
            )
        except Exception as e:
            return {"ok": False, "error": str(e), "trace": traceback.format_exc()}
        if not er.ok:
            return {"ok": False, "error": er.error, "stderr": er.stderr}
        return {"ok": True, "path": str(path)}

    def project_export_combined(self, project_id: str) -> dict:
        """Export every visible object unioned into a single file. Opens
        a save dialog; format is inferred from the chosen extension."""
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        visible = [o for o in proj.list_objects() if o.get("visible", True)]
        if not visible:
            return {"ok": False, "error": "no visible objects to export"}
        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "no window"}
        exports_dir = proj.path / "exports"
        exports_dir.mkdir(parents=True, exist_ok=True)
        result = window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(exports_dir),
            save_filename=f"{proj.title}.stl",
            file_types=(
                "STL 3D printing (*.stl)",
                "STEP parametric CAD (*.step)",
                "3MF modern STL replacement (*.3mf)",
                "BREP OpenCascade native (*.brep)",
            ),
        )
        if not result:
            return {"ok": False, "cancelled": True}
        path = Path(result if isinstance(result, str) else result[0])
        items = [
            {
                "name": o["name"],
                "script": str(proj.object_source_path(o["name"])),
                "params": str(proj.object_params_path(o["name"])),
            }
            for o in visible
        ]
        try:
            er = export_models_script(
                items, path, cwd=proj.path,
                sketches=_sketches_manifest(proj),
                imports=_imports_manifest(proj),
            )
        except Exception as e:
            return {"ok": False, "error": str(e), "trace": traceback.format_exc()}
        if not er.ok:
            return {"ok": False, "error": er.error, "stderr": er.stderr}
        return {"ok": True, "path": str(path), "objects": [o["name"] for o in visible]}

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

    # --- imports -------------------------------------------------------

    def import_pick_and_create(self, project_id: str, name: str | None = None) -> dict:
        """Open a file picker for supported model files, copy the chosen
        file in as a new import, and tessellate it for the viewer.
        `name` overrides the default filename-based name."""
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        picked = self.pick_file([
            "All supported (*.step;*.stp;*.iges;*.igs;*.brep;*.brp;*.stl;*.glb;*.gltf;*.3mf)",
            "STEP (*.step;*.stp)",
            "IGES (*.iges;*.igs)",
            "BREP (*.brep;*.brp)",
            "STL (*.stl)",
            "glTF / GLB (*.glb;*.gltf)",
            "3MF (*.3mf)",
        ])
        if not picked.get("ok"):
            return picked
        try:
            safe = proj.create_import(Path(picked["path"]), name=name)
        except (ValueError, FileExistsError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
        self._emit_import_geometry(proj, safe)
        # Imports may be referenced by visible objects; re-render so the
        # viewer reflects any boolean ops that touch the new import.
        for o in proj.list_objects():
            if o.get("visible", True):
                self._emit_object_geometry(proj, o["name"])
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "name": safe, "project": proj.to_json()}

    def import_rename(self, project_id: str, old: str, new: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            safe = proj.rename_import(old, new)
        except (ValueError, FileExistsError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
        if safe != old:
            self._emit_import_geometry(proj, safe)
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "name": safe, "project": proj.to_json()}

    def import_delete(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.delete_import(name)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        bus.emit("doc_import_geometry", {
            "doc_id": proj.id, "import": name, "ok": False, "deleted": True,
        })
        for o in proj.list_objects():
            if o.get("visible", True):
                self._emit_object_geometry(proj, o["name"])
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return {"ok": True, "project": proj.to_json()}

    def import_set_visible(self, project_id: str, name: str, visible: bool) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.set_import_visible(name, bool(visible))
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        if visible:
            self._emit_import_geometry(proj, name)
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

    # --- browser passthrough ------------------------------------------

    def browser_send_input(self, kind: str, params: dict | None = None) -> dict:
        """Dispatch a single input event (mouse_press / mouse_release /
        mouse_move / key_down / key_up / insert_text / wheel) to the
        embedded Chromium so the user can solve CAPTCHAs / log in /
        otherwise help the agent past a wall."""
        ok = browser_session.session.send_input(str(kind), params or {})
        return {"ok": ok}

    # --- permissions ---------------------------------------------------

    def permission_resolve(self, request_id: str, approved: bool,
                           message: str = "") -> dict:
        """Resolve a pending tool-permission request from the chat. The
        agent's `can_use_tool` callback is blocked on a threading.Event
        for this request; this call sets the result and unblocks it."""
        ok = permissions.store.resolve(request_id, bool(approved), str(message or ""))
        return {"ok": ok}

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
        # Tell the FE we're loading right now so the row can show a
        # spinner instead of staring at a stale frame for the seconds
        # it takes to run the script.
        bus.emit("doc_geometry", {
            "doc_id": project.id, "object": name, "loading": True,
        })
        try:
            result = run_script(
                project.object_source_path(name),
                project.object_params_path(name),
                cwd=project.path,
                timeout=30.0,
                sketches=_sketches_manifest(project),
                imports=_imports_manifest(project),
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

    def _emit_import_geometry(self, project: Project, name: str) -> None:
        """Tessellate one import's STEP file and push GLB to the viewer."""
        source = project.import_source_path(name)
        if source is None:
            bus.emit("doc_import_geometry", {
                "doc_id": project.id,
                "import": name,
                "ok": False,
                "error": f"import '{name}' not found on disk",
            })
            return
        bus.emit("doc_import_geometry", {
            "doc_id": project.id, "import": name, "loading": True,
        })
        try:
            result = tessellate_import_script(source, cwd=project.path, timeout=60.0)
        except Exception as e:
            bus.emit("doc_import_geometry", {
                "doc_id": project.id,
                "import": name,
                "ok": False,
                "error": f"tessellate failed: {e}",
                "trace": traceback.format_exc(),
            })
            return
        bus.emit("doc_import_geometry", {
            "doc_id": project.id,
            "import": name,
            "ok": result.ok,
            "error": result.error,
            "stderr": result.stderr,
            "meta": result.meta,
            "glb_b64": result.glb_b64,
            "topology": result.topology,
        })

    def _emit_sketch_geometry(self, project: Project, name: str) -> None:
        """Tessellate one sketch's wires and push to the viewer overlay."""
        bus.emit("doc_sketch_geometry", {
            "doc_id": project.id, "sketch": name, "loading": True,
        })
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
            "dimensions": result.dimensions,
            "bbox": result.bbox,
        })

    def _emit_all_visible_geometry(self, project: Project) -> None:
        """Render every visible artifact (sketches + imports + objects) and
        emit per-item geometry events. Sketches and imports are emitted
        before objects so the agent's reference geometry has loaded by the
        time the object runner pulls it in."""
        for s in project.list_sketches():
            if s.get("visible", True):
                self._emit_sketch_geometry(project, s["name"])
        for i in project.list_imports():
            if i.get("visible", True):
                self._emit_import_geometry(project, i["name"])
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
