"""Headless Chromium under our control, exposed via CDP.

When the user enables the Playwright integration, two things need to be
true:

  1. `@playwright/mcp` has a browser to drive — we hand it one via
     `--cdp-endpoint=ws://...`.
  2. Our app gets to SEE what the browser is doing — we run a side CDP
     client that subscribes to Page.screencast and forwards frames over
     the bus to the React frontend.

This module manages both: spawn one Chromium, return its CDP endpoint
so the runner can pass it to playwright-mcp, and keep a background
asyncio loop alive that streams screencast frames.

Lifecycle: started lazily on first call to `ensure_started()`; stopped
explicitly via `stop()` or whenever the host process exits (atexit).
The same Chromium is reused across agent turns — restarting Chromium on
every prompt would be slow and would lose page state mid-conversation.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

from .events import bus

log = logging.getLogger(__name__)

# Cap render output so frames stay small over the bus.
SCREENCAST_MAX_W = 1280
SCREENCAST_MAX_H = 800
SCREENCAST_QUALITY = 70  # JPEG quality 0-100
# Send acks immediately — CDP will pause the stream after one un-acked frame.
# We can adjust if frame events flood the bus.

CHROMIUM_FLAGS = [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate",
    "--disable-popup-blocking",
    "--disable-background-networking",
    f"--window-size={SCREENCAST_MAX_W},{SCREENCAST_MAX_H}",
]


def _pick_free_port() -> int:
    """Bind a socket to OS-assigned port 0 and immediately close to learn
    which port the OS would have given us. Tiny race window between this
    and the Chromium spawn; rare in practice on a desktop.

    We pick the port up-front so we can hand the resulting CDP URL to
    @playwright/mcp at MCP-server-spawn time, even though we won't
    actually start Chromium until the first Playwright tool fires."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _candidate_chromium_paths() -> list[Path]:
    """Where to look for a Chromium binary on Windows. We try the
    Playwright cache first since that's the version `@playwright/mcp`
    will know how to drive, then fall back to system Chrome / Edge."""
    out: list[Path] = []
    env = os.environ.get("AGENTCAD_CHROMIUM")
    if env:
        out.append(Path(env))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        ms_pw = Path(local) / "ms-playwright"
        if ms_pw.is_dir():
            for child in sorted(ms_pw.glob("chromium-*"), reverse=True):
                out.append(child / "chrome-win" / "chrome.exe")
            for child in sorted(ms_pw.glob("chromium_headless_shell-*"), reverse=True):
                out.append(child / "chrome-win" / "headless_shell.exe")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    out.extend([
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ])
    return out


def _find_chromium() -> Path | None:
    for p in _candidate_chromium_paths():
        if p.exists():
            return p
    # Last resort: PATH lookup.
    for name in ("chrome", "chromium", "msedge"):
        which = shutil.which(name)
        if which:
            return Path(which)
    return None


class BrowserSession:
    """Process-wide Chromium controller. Thread-safe — start/stop and
    `ensure_started()` can be called from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._user_data_dir: Path | None = None
        self._port: int | None = None
        self._cdp_http: str | None = None
        self._cdp_ws_browser: str | None = None
        self._cdp_thread: threading.Thread | None = None
        self._stop_evt: threading.Event = threading.Event()
        self._chromium_path: Path | None = None
        # Set by the screencast loop once it's up, used by send_input()
        # to dispatch CDP Input.* commands from outside the loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._active_session_id: str | None = None
        self._next_msg_id = 1000

    @property
    def cdp_http_endpoint(self) -> str | None:
        return self._cdp_http

    @property
    def cdp_ws_browser_endpoint(self) -> str | None:
        return self._cdp_ws_browser

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def reserve_port(self) -> int:
        """Reserve (but don't yet bind) a CDP port. Returns the same port
        across calls until Chromium is started + stopped.

        Used by the runner so it can hand --cdp-endpoint to
        @playwright/mcp at MCP-spawn time, even though we wait until the
        first Playwright tool actually fires before launching Chromium.
        """
        with self._lock:
            if self._port is not None:
                return self._port
            self._port = _pick_free_port()
            self._cdp_http = f"http://127.0.0.1:{self._port}"
            return self._port

    def ensure_started(self, *, timeout: float = 20.0) -> str | None:
        """Spawn Chromium if it isn't running, wait for the CDP port to be
        ready, kick off the screencast forwarder, and return the CDP HTTP
        endpoint URL (e.g. http://127.0.0.1:5234). Returns None if
        Chromium can't be found at all — caller should fall back to
        letting playwright-mcp launch its own browser."""
        with self._lock:
            if self.is_running and self._cdp_http:
                return self._cdp_http

            chromium = self._chromium_path or _find_chromium()
            if chromium is None:
                log.warning("could not find a Chromium binary for the browser session")
                return None
            self._chromium_path = chromium

            # Use the previously-reserved port if one exists; otherwise pick
            # one now. Reservation up-front lets the runner pass a stable
            # --cdp-endpoint to MCP before this point.
            if self._port is None:
                self._port = _pick_free_port()
                self._cdp_http = f"http://127.0.0.1:{self._port}"

            user_data_dir = Path(tempfile.mkdtemp(prefix="agentcad-browser-"))
            self._user_data_dir = user_data_dir
            cmd = [
                str(chromium),
                f"--user-data-dir={user_data_dir}",
                f"--remote-debugging-port={self._port}",
                *CHROMIUM_FLAGS,
                "about:blank",
            ]
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Chromium writes its actual port to DevToolsActivePort once
            # it's up. Wait briefly for it to come online.
            port_file = user_data_dir / "DevToolsActivePort"
            deadline = time.time() + timeout
            ready = False
            while time.time() < deadline:
                if self._proc.poll() is not None:
                    break  # crashed
                if port_file.exists():
                    ready = True
                    break
                time.sleep(0.1)
            if not ready:
                log.warning("Chromium failed to come online on the reserved port")
                self._cleanup_after_failure()
                return None

            # Resolve the browser WS URL via /json/version.
            try:
                with httpx.Client(timeout=5.0) as c:
                    info = c.get(f"{self._cdp_http}/json/version").json()
                self._cdp_ws_browser = info.get("webSocketDebuggerUrl")
            except Exception as e:
                log.warning(f"could not fetch CDP /json/version: {e}")
                self._cdp_ws_browser = None

            # Kick off the screencast forwarder thread.
            self._stop_evt.clear()
            self._cdp_thread = threading.Thread(
                target=self._screencast_forever,
                name="agentcad-cdp",
                daemon=True,
            )
            self._cdp_thread.start()

            log.info(f"browser session started on {self._cdp_http}")
            return self._cdp_http

    def stop(self) -> None:
        """Cleanly shut Chromium down."""
        with self._lock:
            self._stop_evt.set()
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
            self._proc = None
            self._cdp_http = None
            self._cdp_ws_browser = None
            self._port = None
            self._loop = None
            self._ws = None
            self._active_session_id = None
            if self._user_data_dir is not None:
                # Best-effort cleanup; Chromium may still hold handles
                # for a moment on Windows.
                shutil.rmtree(self._user_data_dir, ignore_errors=True)
                self._user_data_dir = None
            self._cdp_thread = None

    # --- internals ----------------------------------------------------

    def _cleanup_after_failure(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        self._proc = None
        if self._user_data_dir is not None:
            shutil.rmtree(self._user_data_dir, ignore_errors=True)
            self._user_data_dir = None
        self._port = None
        self._cdp_http = None
        self._cdp_ws_browser = None

    def _screencast_forever(self) -> None:
        """Run the asyncio CDP client on a dedicated thread loop."""
        try:
            asyncio.run(self._screencast_loop())
        except Exception as e:
            log.warning(f"browser screencast loop terminated: {e}")

    async def _screencast_loop(self) -> None:
        """Connect to the browser WS, auto-attach to all page targets,
        and stream Page.screencastFrame events over the bus."""
        # Wait for the browser WS URL — it may be None if /json/version
        # failed; in that case the panel just won't show anything but the
        # agent still gets to use Playwright via the MCP server.
        if not self._cdp_ws_browser:
            return

        self._loop = asyncio.get_running_loop()

        def _build_msg(method: str, params: dict | None = None,
                       session_id: str | None = None) -> str:
            with self._lock:
                mid = self._next_msg_id
                self._next_msg_id += 1
            payload: dict[str, Any] = {"id": mid, "method": method,
                                       "params": params or {}}
            if session_id is not None:
                payload["sessionId"] = session_id
            return json.dumps(payload)

        msg = lambda *a, **kw: (None, _build_msg(*a, **kw))  # noqa: E731 — keeps existing call shape

        try:
            async with websockets.connect(
                self._cdp_ws_browser,
                max_size=64 * 1024 * 1024,  # frames can be ~MB at high quality
                ping_interval=20,
            ) as ws:
                self._ws = ws
                # Discover all targets + auto-attach to new ones, flatten
                # so we get sessionIds inline rather than nested.
                _, m = msg("Target.setDiscoverTargets", {"discover": True})
                await ws.send(m)
                _, m = msg("Target.setAutoAttach", {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": False,
                    "flatten": True,
                })
                await ws.send(m)
                # Also explicitly attach to any existing targets — the
                # auto-attach above only catches future ones in some cases.
                _, m = msg("Target.getTargets")
                await ws.send(m)

                # Set of session IDs we've already started screencasting on.
                started_sessions: set[str] = set()

                while not self._stop_evt.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # `Target.attachedToTarget` arrives without a sessionId
                    # at the message top-level; we read params.sessionId.
                    if data.get("method") == "Target.attachedToTarget":
                        params = data.get("params") or {}
                        info = params.get("targetInfo") or {}
                        if info.get("type") != "page":
                            continue
                        sid = params.get("sessionId")
                        if not sid or sid in started_sessions:
                            continue
                        started_sessions.add(sid)
                        # Most-recently-attached page is the input target.
                        self._active_session_id = sid
                        # Page.enable + Page.startScreencast on the new session.
                        _, m = msg("Page.enable", session_id=sid)
                        await ws.send(m)
                        _, m = msg("Page.startScreencast", {
                            "format": "jpeg",
                            "quality": SCREENCAST_QUALITY,
                            "maxWidth": SCREENCAST_MAX_W,
                            "maxHeight": SCREENCAST_MAX_H,
                            "everyNthFrame": 1,
                        }, session_id=sid)
                        await ws.send(m)
                        # Surface a "browser opened" event so the FE can
                        # show the panel even before the first frame.
                        bus.emit("playwright_frame", {
                            "kind": "session_started",
                            "session_id": sid,
                            "url": info.get("url") or "",
                            "title": info.get("title") or "",
                        })
                        continue

                    if data.get("method") == "Page.screencastFrame":
                        sid = data.get("sessionId")
                        # Track the session we're actively rendering so
                        # input forwarding hits the right page when there
                        # are multiple tabs open.
                        if sid:
                            self._active_session_id = sid
                        params = data.get("params") or {}
                        frame_b64 = params.get("data")
                        meta = params.get("metadata") or {}
                        session_no = params.get("sessionId")
                        if frame_b64:
                            bus.emit("playwright_frame", {
                                "kind": "frame",
                                "session_id": sid,
                                "data": frame_b64,
                                "mime": "image/jpeg",
                                "device_width": meta.get("deviceWidth"),
                                "device_height": meta.get("deviceHeight"),
                                "page_scale_factor": meta.get("pageScaleFactor"),
                            })
                        # Ack so Chromium keeps sending.
                        if session_no is not None and sid:
                            _, m = msg("Page.screencastFrameAck",
                                       {"sessionId": session_no},
                                       session_id=sid)
                            try:
                                await ws.send(m)
                            except Exception:
                                pass
                        continue

                    if data.get("method") == "Page.frameNavigated":
                        sid = data.get("sessionId")
                        params = data.get("params") or {}
                        frame = params.get("frame") or {}
                        # Only the main frame matters for the FE banner.
                        if frame.get("parentId"):
                            continue
                        bus.emit("playwright_frame", {
                            "kind": "navigated",
                            "session_id": sid,
                            "url": frame.get("url") or "",
                        })
                        continue

                    if data.get("method") == "Target.detachedFromTarget":
                        params = data.get("params") or {}
                        sid = params.get("sessionId")
                        if sid:
                            started_sessions.discard(sid)
                            if self._active_session_id == sid:
                                # Fall back to any other still-attached page.
                                self._active_session_id = (
                                    next(iter(started_sessions), None)
                                )
                            bus.emit("playwright_frame", {
                                "kind": "session_ended",
                                "session_id": sid,
                            })
                        continue

        except Exception as e:
            log.warning(f"CDP loop error: {e}")
        finally:
            self._ws = None
            self._active_session_id = None

    # --- input forwarding --------------------------------------------

    def send_input(self, kind: str, params: dict[str, Any]) -> bool:
        """Schedule an Input.* CDP command on the screencast loop. Called
        from the JsApi thread when the user interacts with the embedded
        browser panel. Returns False if the browser isn't running yet."""
        loop = self._loop
        ws = self._ws
        sid = self._active_session_id
        if loop is None or ws is None or sid is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self._dispatch_input(kind, params, sid), loop,
            )
            return True
        except RuntimeError:
            return False

    async def _dispatch_input(self, kind: str, params: dict[str, Any],
                              session_id: str) -> None:
        ws = self._ws
        if ws is None:
            return
        method, cdp_params = _input_to_cdp(kind, params)
        if method is None:
            return
        with self._lock:
            mid = self._next_msg_id
            self._next_msg_id += 1
        payload = {
            "id": mid,
            "method": method,
            "params": cdp_params,
            "sessionId": session_id,
        }
        try:
            await ws.send(json.dumps(payload))
        except Exception as e:
            log.warning(f"input dispatch failed: {e}")


# --- frontend-input → CDP translation -------------------------------------
#
# The frontend's panel emits high-level events ("mouse_press at (x, y)",
# "insert_text 'hello'", "key_down Enter") and we map them to the CDP
# Input.* command shape. Coordinates arrive in device pixels — the panel
# scales mouse offsets by the frame's deviceWidth/deviceHeight before
# sending so we don't need to know the panel's display size here.

_KEY_TO_VK = {
    # Subset that covers the common "I need to fill a CAPTCHA / login"
    # cases. Anything not listed falls through with vkCode 0; CDP usually
    # accepts that for char-only events.
    "Enter": 13, "Tab": 9, "Backspace": 8, "Escape": 27,
    "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
    "Delete": 46, "Space": 32,
}


def _input_to_cdp(kind: str, params: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Translate a UI input spec into a (method, params) pair for CDP."""
    if kind in ("mouse_press", "mouse_release", "mouse_move"):
        cdp_type = {
            "mouse_press": "mousePressed",
            "mouse_release": "mouseReleased",
            "mouse_move": "mouseMoved",
        }[kind]
        return "Input.dispatchMouseEvent", {
            "type": cdp_type,
            "x": float(params.get("x", 0)),
            "y": float(params.get("y", 0)),
            "button": str(params.get("button") or "left"),
            "buttons": int(params.get("buttons", 1 if kind != "mouse_release" else 0)),
            "clickCount": int(params.get("click_count") or 1),
        }
    if kind in ("key_down", "key_up"):
        cdp_type = "keyDown" if kind == "key_down" else "keyUp"
        key = str(params.get("key") or "")
        code = str(params.get("code") or "")
        body: dict[str, Any] = {
            "type": cdp_type,
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": _KEY_TO_VK.get(key, 0),
        }
        # For printable characters, sending text on keyDown helps the
        # page see a complete keypress.
        if cdp_type == "keyDown" and len(key) == 1:
            body["text"] = key
            body["unmodifiedText"] = key
        return "Input.dispatchKeyEvent", body
    if kind == "insert_text":
        return "Input.insertText", {"text": str(params.get("text") or "")}
    if kind == "wheel":
        return "Input.dispatchMouseEvent", {
            "type": "mouseWheel",
            "x": float(params.get("x", 0)),
            "y": float(params.get("y", 0)),
            "deltaX": float(params.get("delta_x", 0)),
            "deltaY": float(params.get("delta_y", 0)),
        }
    return None, {}


# Process-wide singleton.
session = BrowserSession()
atexit.register(session.stop)
