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
from .printing import (
    PRESETS,
    PRINTER_KINDS,
    PhaseState,
    SliceOverride,
    build_printer,
    build_slicer,
)
from .printing.presets import DEFAULT_PRESET, lookup as lookup_preset


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
        # Per-project print-phase state. Empty for any project not
        # currently in the print phase. Lifetime is tied to the JsApi
        # (process lifetime), not persisted to disk — leaving the print
        # phase clears it.
        self._print_phase = PhaseState()

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
            extra_context=self._agent_extra_context(proj),
            print_api=self,
        )
        return {"ok": True}

    # --- print phase ---------------------------------------------------
    #
    # The print phase is a separate UI mode the user opts into. The
    # frontend takes over the viewer area; the chat panel stays
    # visible and the agent gets a phase-aware prompt block plus a
    # handful of print-only tools. State lives in `self._print_phase`
    # for the lifetime of the JsApi.

    def print_phase_get(self, project_id: str) -> dict:
        """Snapshot of phase state for a project. Always succeeds —
        when the project isn't in the phase the response is `active=False`
        with no session payload."""
        s = settings.load()
        printers_payload = self._printers_payload(s)
        active = self._print_phase.is_active(project_id)
        session = self._print_phase.get(project_id)
        return {
            "ok": True,
            "active": active,
            "session": session.to_json() if session else None,
            "printers": printers_payload,
            "default_printer_id": s.default_printer_id,
            "presets": [
                {"id": p.id, "label": p.label, "description": p.description}
                for p in PRESETS
            ],
        }

    def print_phase_enter(self, project_id: str,
                          preset: str | None = None) -> dict:
        """Transition the project into the print phase.

        Exports every visible object into a single 3MF inside the
        project's `.agentcad-cache/print/` folder, then runs the slicer
        with the selected preset (defaulting to standard). The slicer
        auto-orients the part as part of its first pass."""
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        s = settings.load()
        if not s.printers:
            return {"ok": False, "error": (
                "no 3D printer is configured. Open Settings → Printers and "
                "add one before entering the print phase."
            )}
        session = self._print_phase.enter(project_id)
        if preset:
            try:
                lookup_preset(preset)
                session.preset = preset
            except KeyError as e:
                return {"ok": False, "error": str(e)}
        if not session.printer_id:
            session.printer_id = s.default_printer_id or s.printers[0]["id"]
        # Fire off a printer-state query in the background so the UI
        # can render the phase immediately without blocking on the ~3s
        # MQTT round-trip. The query updates session.printer_state and
        # emits a fresh print_state event when it lands.
        import threading as _threading  # noqa: PLC0415
        _threading.Thread(
            target=self._refresh_printer_state_async,
            args=(project_id,),
            daemon=True,
            name="cad-printer-state",
        ).start()
        self._emit_print_state(project_id)
        return self.print_phase_get(project_id)

    def _refresh_printer_state_async(self, project_id: str) -> None:
        """Background printer-state refresh. Swallows exceptions —
        a failed query just leaves session.printer_state as None and
        the UI shows 'querying…' indefinitely. We re-emit a print_state
        event so the frontend picks up the populated snapshot."""
        try:
            self.print_query_printer_state(project_id)
        except Exception:
            self._emit_print_state(project_id)

    def print_phase_leave(self, project_id: str) -> dict:
        self._print_phase.leave(project_id)
        bus.emit("print_state", {"doc_id": project_id, "active": False})
        return {"ok": True}

    def print_set_preset(self, project_id: str, preset: str) -> dict:
        session = self._print_phase.get(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        try:
            lookup_preset(preset)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        session.preset = preset
        # Changing the preset invalidates the previous slice — UI hides
        # filament / time numbers until re-slicing.
        session.last_slice = None
        self._emit_print_state(project_id)
        return {"ok": True, "session": session.to_json()}

    def print_set_printer(self, project_id: str, printer_id: str) -> dict:
        session = self._print_phase.get(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        s = settings.load()
        if not any(p.get("id") == printer_id for p in s.printers):
            return {"ok": False, "error": f"unknown printer {printer_id!r}"}
        session.printer_id = printer_id
        self._emit_print_state(project_id)
        return {"ok": True, "session": session.to_json()}

    def print_set_overrides(self, project_id: str,
                             overrides: list[dict] | None = None) -> dict:
        """Replace the override list wholesale. Each override is
        `{key, value, note?}`. Used by both the agent (apply / clear)
        and the UI (manual edits)."""
        session = self._print_phase.get(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        new: list[SliceOverride] = []
        for ov in (overrides or []):
            if not isinstance(ov, dict):
                continue
            key = str(ov.get("key", "")).strip()
            if not key:
                continue
            new.append(SliceOverride(
                key=key,
                value=str(ov.get("value", "")).strip(),
                note=str(ov.get("note", "") or ""),
            ))
        session.overrides = new
        session.last_slice = None
        self._emit_print_state(project_id)
        return {"ok": True, "session": session.to_json()}

    def print_slice(self, project_id: str) -> dict:
        """Export the current visible objects, then slice with the
        current preset + overrides. Updates session.last_slice."""
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        session = self._print_phase.get(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}

        cache = proj.path / ".agentcad-cache" / "print"
        cache.mkdir(parents=True, exist_ok=True)
        export_path = cache / "model.3mf"
        # Export every visible object as one combined 3MF — cheaper for the
        # slicer (one part, one orientation pass) and gives Bambu Studio
        # the structure it needs (3MF carries assemblies natively).
        visible = [o for o in proj.list_objects() if o.get("visible", True)]
        if not visible:
            return {"ok": False, "error": "no visible objects to print"}
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
                items, export_path, cwd=proj.path,
                sketches=_sketches_manifest(proj),
                imports=_imports_manifest(proj),
            )
        except Exception as e:
            return {"ok": False, "error": str(e), "trace": traceback.format_exc()}
        if not er.ok:
            return {"ok": False, "error": er.error or "export failed"}
        session.last_export_path = str(export_path)

        # Build the slicer + printer hint, then run.
        s = settings.load()
        slicer = build_slicer("bambu_studio", {
            "cli_path": s.bambu_studio_cli_path,
        })
        printer_hint = self._build_printer_hint(session, s)

        result = slicer.auto_orient_and_slice(
            [export_path],
            preset=session.preset,
            overrides=session.overrides,
            out_dir=cache,
            printer_hint=printer_hint or None,
        )
        session.last_slice = result
        self._emit_print_state(project_id)
        return {"ok": result.ok, "session": session.to_json()}

    def print_query_printer_state(self, project_id: str) -> dict:
        """Refresh the live snapshot from the printer (filament + bed
        type + nozzle). The result lands on session.printer_state and
        is fed into subsequent slices automatically."""
        session = self._print_phase.get(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        if not session.printer_id:
            return {"ok": False, "error": "no printer selected"}
        s = settings.load()
        cfg = next((p for p in s.printers if p.get("id") == session.printer_id), None)
        if cfg is None:
            return {"ok": False, "error": f"printer {session.printer_id!r} not configured"}
        try:
            printer = build_printer(cfg.get("kind", "bambu_x1c"), cfg)
        except Exception as e:
            return {"ok": False, "error": f"printer config invalid: {e}"}
        # MQTT pushall takes a couple of seconds in the worst case.
        state = printer.get_state(timeout=6.0)
        session.printer_state = state
        # Slice estimates are tied to the previous filament/plate; flush.
        if state.online and not state.error:
            session.last_slice = None
        self._emit_print_state(project_id)
        return {"ok": state.online, "state": state.to_json()}

    def print_send(self, project_id: str) -> dict:
        """Send the currently-sliced job to the configured printer."""
        session = self._print_phase.get(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        if session.last_slice is None or not session.last_slice.ok:
            return {"ok": False, "error": "no successful slice — slice first"}
        if not session.printer_id:
            return {"ok": False, "error": "no printer selected"}
        s = settings.load()
        cfg = next((p for p in s.printers if p.get("id") == session.printer_id), None)
        if cfg is None:
            return {"ok": False, "error": f"printer {session.printer_id!r} no longer configured"}
        try:
            printer = build_printer(cfg.get("kind", "bambu_x1c"), cfg)
        except Exception as e:
            return {"ok": False, "error": f"could not init printer: {e}"}
        ok, msg = printer.send_print(Path(session.last_slice.sliced_path))
        session.last_send_ok = ok
        session.last_send_message = msg
        self._emit_print_state(project_id)
        return {"ok": ok, "message": msg, "session": session.to_json()}

    def print_test_printer(self, printer_id: str) -> dict:
        """Diagnostic — test reachability without entering print phase.
        Used by the Settings dialog's "Test connection" button."""
        s = settings.load()
        cfg = next((p for p in s.printers if p.get("id") == printer_id), None)
        if cfg is None:
            return {"ok": False, "error": f"unknown printer {printer_id!r}"}
        try:
            printer = build_printer(cfg.get("kind", "bambu_x1c"), cfg)
        except Exception as e:
            return {"ok": False, "error": f"bad config: {e}"}
        ok, why = printer.is_available()
        status = printer.status().to_json() if ok else None
        return {"ok": ok, "message": why, "status": status}

    def slicer_diagnose(self) -> dict:
        """Return whether the slicer CLI is available + the path we'd use."""
        s = settings.load()
        slicer = build_slicer("bambu_studio", {"cli_path": s.bambu_studio_cli_path})
        ok, info = slicer.is_available()
        return {"ok": ok, "message": info if ok else None, "error": None if ok else info}

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

    def _printers_payload(self, s: "settings.Settings") -> list[dict]:
        """Sanitised printer list for the UI — strips the access code so
        it's never round-tripped through the JS layer."""
        out: list[dict] = []
        for p in s.printers:
            out.append({
                "id": p.get("id"),
                "name": p.get("name") or p.get("id"),
                "kind": p.get("kind", "bambu_x1c"),
                "ip": p.get("ip", ""),
                "serial": p.get("serial", ""),
                "has_access_code": bool(p.get("access_code")),
                "printer_profile": p.get("printer_profile", ""),
                "process_profile": p.get("process_profile", ""),
                "filament_profile": p.get("filament_profile", ""),
                "default_bed_type": p.get("default_bed_type", "Textured PEI Plate"),
            })
        return out

    def _build_printer_hint(self, session, s: "settings.Settings") -> dict:
        """Combine static printer config + live MQTT state into the
        slicer's `printer_hint`. Detected fields override config-set
        fallbacks; missing fields stay empty so slicer defaults apply."""
        if not session.printer_id:
            return {}
        cfg = next((p for p in s.printers if p.get("id") == session.printer_id), None)
        if cfg is None:
            return {}
        hint: dict = {
            "printer_profile": cfg.get("printer_profile", ""),
            "process_profile": cfg.get("process_profile", ""),
            "filament_profile": cfg.get("filament_profile", ""),
            "default_bed_type": cfg.get("default_bed_type", "Textured PEI Plate"),
        }
        ps = session.printer_state
        if ps and ps.online:
            active = ps.active_slot()
            if active:
                hint["detected_tray_type"] = active.type
                hint["detected_tray_info_idx"] = active.tray_info_idx
            if ps.bed_type_slicer:
                hint["detected_bed_type_slicer"] = ps.bed_type_slicer
        return hint

    def _emit_print_state(self, project_id: str) -> None:
        session = self._print_phase.get(project_id)
        bus.emit("print_state", {
            "doc_id": project_id,
            "active": session is not None,
            "session": session.to_json() if session else None,
        })

    def _agent_extra_context(self, project: Project) -> dict:
        """Per-turn context the agent runner reads to tweak its prompt
        + tool surface. Currently surfaces the print phase."""
        session = self._print_phase.get(project.id)
        if session is None:
            return {}
        s = settings.load()
        printer_cfg = None
        if session.printer_id:
            printer_cfg = next(
                (p for p in s.printers if p.get("id") == session.printer_id),
                None,
            )
        return {
            "print_phase": {
                "active": True,
                "preset": session.preset,
                "overrides": [o.to_json() for o in session.overrides],
                "last_slice": session.last_slice.to_json() if session.last_slice else None,
                "printer": {
                    "id": printer_cfg.get("id") if printer_cfg else None,
                    "name": printer_cfg.get("name") if printer_cfg else None,
                    "kind": printer_cfg.get("kind") if printer_cfg else None,
                } if printer_cfg else None,
            }
        }

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
