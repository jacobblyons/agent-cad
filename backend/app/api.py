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
from .cad.script_runner import RunResult, run as run_script
from .events import bus


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
        self._run_and_emit(project)
        return {"ok": True, "project": project.to_json()}

    def project_open(self, path: str) -> dict:
        try:
            project = Project.open(Path(path))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._projects[project.id] = project
        self._run_and_emit(project)
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
        if name == proj.active_object():
            self._run_and_emit(proj)
        return {"ok": True}

    def project_set_parameter(self, project_id: str, name: str, value: float,
                              object_name: str | None = None) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        target = object_name or proj.active_object()
        params = proj.read_object_params(target)
        params[name] = float(value)
        proj.write_object_params(target, params)
        if target == proj.active_object():
            self._run_and_emit(proj)
        return {"ok": True, "params": params}

    def project_refresh(self, project_id: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        self._run_and_emit(proj)
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
        self._run_and_emit(proj)
        return {"ok": True, "name": safe, "project": proj.to_json()}

    def object_rename(self, project_id: str, old: str, new: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            safe = proj.rename_object(old, new)
        except (ValueError, FileExistsError, FileNotFoundError) as e:
            return {"ok": False, "error": str(e)}
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
        # delete may have switched active object → re-render.
        self._run_and_emit(proj)
        return {"ok": True, "project": proj.to_json()}

    def object_set_active(self, project_id: str, name: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.set_active_object(name)
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        self._run_and_emit(proj)
        return {"ok": True, "project": proj.to_json()}

    # --- timeline ------------------------------------------------------

    def timeline_checkout(self, project_id: str, ref: str) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            proj.checkout(ref)
            self._run_and_emit(proj)
            return {"ok": True, "project": proj.to_json()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def timeline_branch(self, project_id: str, ref: str, name: str | None = None) -> dict:
        proj = self._projects.get(project_id)
        if proj is None:
            return {"ok": False, "error": "not_found"}
        try:
            branch = proj.branch_at(ref, name)
            self._run_and_emit(proj)
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

    def _run_and_emit(self, project: Project) -> None:
        active = project.active_object()
        try:
            result = run_script(
                project.object_source_path(active),
                project.object_params_path(active),
                cwd=project.path,
                timeout=30.0,
            )
        except Exception as e:
            bus.emit("doc_geometry", {
                "doc_id": project.id,
                "object": active,
                "error": f"run failed: {e}",
                "trace": traceback.format_exc(),
            })
            bus.emit("project_state", {"doc_id": project.id, "state": project.to_json()})
            return
        self._emit_run(project, result)

    def _emit_run(self, project: Project, result: RunResult) -> None:
        bus.emit("doc_geometry", {
            "doc_id": project.id,
            "object": project.active_object(),
            "ok": result.ok,
            "error": result.error,
            "stderr": result.stderr,
            "meta": result.meta,
            "glb_b64": result.glb_b64,
            "topology": result.topology,
        })
        bus.emit("project_state", {"doc_id": project.id, "state": project.to_json()})
