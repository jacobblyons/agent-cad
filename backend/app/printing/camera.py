"""Single-frame grabber for the Bambu printer's chamber camera.

The X1C exposes its chamber cam as a self-signed RTSPS stream on port
322 (`rtsps://bblp:<access_code>@<ip>:322/streaming/live/1`). We read
it through OpenCV's bundled FFmpeg, throw away the first few decoded
frames (the very first decoded frame on RTSPS is sometimes black or
partial — the codec needs a keyframe before it can produce a clean
image), and return the last good one as PNG bytes.

Both the agent's MCP `printer_camera_snapshot` tool and the
`backend/scripts/printer_snapshot.py` CLI use this — pulling the logic
into one place keeps them in sync (FFmpeg flags, retry counts, etc.).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from app import settings as app_settings


@dataclass
class CameraResult:
    ok: bool
    png_bytes: bytes | None = None
    error: str = ""
    printer_id: str = ""
    printer_name: str = ""


def grab_frame(
    *,
    printer_id: str | None = None,
    frames: int = 15,
    timeout: float = 10.0,
) -> CameraResult:
    """Grab one PNG-encoded frame from a configured printer's camera.

    `printer_id` defaults to `default_printer_id` from settings.
    `frames` is how many decoded frames to consume before keeping the
    last one. `timeout` is a hard cap on the read loop.
    """
    s = app_settings.load()
    pid = printer_id or s.default_printer_id
    if not pid:
        return CameraResult(ok=False, error="no printer configured (and no printer_id specified)")
    cfg = next((p for p in s.printers if p.get("id") == pid), None)
    if cfg is None:
        return CameraResult(ok=False, error=f"printer {pid!r} not configured")

    name = cfg.get("name") or pid
    ip = cfg.get("ip")
    access_code = cfg.get("access_code")
    if not ip or not access_code:
        return CameraResult(
            ok=False,
            printer_id=pid,
            printer_name=name,
            error=("printer is missing ip / access_code "
                   "(turn on Developer Mode on the X1C and set them in Settings)"),
        )

    # opencv-python-headless is bundled but heavy — defer import so the
    # rest of the printing module loads fast in environments that don't
    # have it (CI, --no-printer test setups, etc).
    try:
        import cv2  # noqa: PLC0415
    except ImportError as e:
        return CameraResult(
            ok=False, printer_id=pid, printer_name=name,
            error=f"opencv-python-headless not installed: {e}",
        )

    url = f"rtsps://bblp:{access_code}@{ip}:322/streaming/live/1"

    # FFmpeg (the backend OpenCV uses for RTSPS) needs three knobs:
    #   - rtsp_transport;tcp        UDP often gets dropped on home routers
    #   - tls_verify;0              the X1C uses a self-signed cert
    #   - allowed_media_types;video skip audio negotiation (X1C has none)
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
        "rtsp_transport;tcp|tls_verify;0|allowed_media_types;video"
    )

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        return CameraResult(
            ok=False, printer_id=pid, printer_name=name,
            error=f"failed to open RTSPS stream at {ip}:322 — printer may be off "
                  f"or on a different LAN, or the access code is stale",
        )

    deadline = time.time() + timeout
    frame = None
    try:
        for _ in range(frames):
            if time.time() >= deadline:
                break
            got, f = cap.read()
            if got and f is not None and f.size:
                frame = f
    finally:
        cap.release()

    if frame is None:
        return CameraResult(
            ok=False, printer_id=pid, printer_name=name,
            error="no frames decoded — printer may be in a state that disables "
                  "the camera (mid-pause-error, lid-open lockout, etc.)",
        )

    ok, buf = cv2.imencode(".png", frame)
    if not ok:
        return CameraResult(
            ok=False, printer_id=pid, printer_name=name,
            error="cv2.imencode failed",
        )

    return CameraResult(
        ok=True,
        png_bytes=bytes(buf),
        printer_id=pid,
        printer_name=name,
    )
