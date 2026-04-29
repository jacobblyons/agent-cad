"""Printer drivers — send a sliced job to a physical machine and report status.

A `Printer` knows how to upload a sliced file (typically a .3mf or
.gcode) to one specific 3D printer and start a print. Implementations
are vendor-specific; abstraction lets us add OctoPrint, Klipper /
Moonraker, PrusaLink, etc. later without touching the UI.

Current implementations:
    - BambuLabPrinter — talks to a Bambu X1/X1C/P1S/A1 in LAN /
      developer mode using FTPS upload + MQTT print start.

This module degrades gracefully when the optional `bambulabs-api`
package isn't installed; the connection / send methods just return an
error explaining how to install it.
"""
from __future__ import annotations

import ftplib
import socket
import ssl
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

PRINTER_KINDS = ["bambu_x1c"]


@dataclass
class PrinterStatus:
    """Best-effort status snapshot from the printer."""
    online: bool
    state: str = ""           # "idle" / "printing" / "paused" / "error" / ...
    progress_pct: float | None = None
    nozzle_c: float | None = None
    bed_c: float | None = None
    file: str | None = None   # name of the file currently printing
    message: str = ""         # human-readable summary ("4m to layer 50/120")

    def to_json(self) -> dict:
        return asdict(self)


class Printer(ABC):
    kind: str
    id: str
    name: str

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """(ok, reason). When ok=False, reason explains the missing
        prerequisite — credentials, library, or network reach."""

    @abstractmethod
    def status(self) -> PrinterStatus:
        ...

    @abstractmethod
    def send_print(
        self, sliced_path: Path, *, plate_index: int = 1,
        use_ams: bool = False, ams_mapping: list[int] | None = None,
    ) -> tuple[bool, str]:
        """Upload `sliced_path` and start printing. Returns (ok, message).
        On success the printer should be actively printing or queued
        immediately; on failure `message` explains what to fix."""

    @abstractmethod
    def slicer_hint(self) -> dict:
        """Hint dict the slicer consumes so it picks the right machine /
        process / filament profiles. Empty dict is a valid no-op."""


# --------------------------------------------------------------------- #
# Bambu Labs LAN / developer mode driver                                #
# --------------------------------------------------------------------- #


@dataclass
class BambuPrinterConfig:
    """Per-printer config the user enters in Settings.

    `serial` is the SN printed on the back of the machine (start with
    "01S00A...", "00M..." etc.). `access_code` is the LAN access code
    shown on the printer's screen under Settings → WLAN → ... → Access
    Code (only available when developer mode / LAN-only mode is on).
    `ip` is the printer's LAN IP. The slicer profile fields let the
    user pick which Bambu Studio profile JSON corresponds to *this*
    printer (e.g. an X1C with 0.4mm hardened steel)."""
    id: str
    name: str
    kind: str = "bambu_x1c"        # for now only X1C; field reserved for X1, P1S, A1, ...
    ip: str = ""
    serial: str = ""
    access_code: str = ""
    printer_profile: str = ""      # Bambu Studio JSON profile name, e.g. "Bambu Lab X1 Carbon 0.4 nozzle"
    process_profile: str = ""
    filament_profile: str = ""

    def to_json(self) -> dict:
        return asdict(self)


# Bambu's LAN protocol uses an *implicit* TLS FTP on port 990 with the
# username "bblp" and the access code as the password. Files go into
# the `model/` (and sometimes `cache/`) directory on the SD card. After
# upload, an MQTT publish on `device/<sn>/request` with a print/project_file
# command starts the print. We implement the FTP half ourselves (it's
# small) and delegate MQTT to bambulabs-api when installed; if it's
# missing we still upload and tell the user to start the print manually
# from the printer screen.


class _ImplicitFTP_TLS(ftplib.FTP_TLS):
    """ftplib doesn't support implicit TLS out of the box — the standard
    library only knows explicit (AUTH TLS after PLAIN connect). Bambu
    uses port 990 implicit, so we have to wrap the socket in TLS *before*
    the first read. This patched class is the documented workaround
    (Python issue 31662) and the same one bambulabs-api uses internally."""
    @property
    def sock(self) -> socket.socket | None:  # type: ignore[override]
        return self._sock

    @sock.setter
    def sock(self, value: socket.socket | None) -> None:
        if value is not None and self.context and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value


class BambuLabPrinter(Printer):
    """Bambu Lab printer driver (LAN mode).

    Requires the printer to be in developer / LAN mode. With the X1C in
    "Developer Mode" the access-code login is enabled and FTPS / MQTT
    accept connections from the local network without going through
    Bambu's cloud."""
    kind = "bambu_x1c"

    def __init__(self, config: BambuPrinterConfig | dict):
        if isinstance(config, dict):
            config = BambuPrinterConfig(**config)
        self.config = config
        self.id = config.id
        self.name = config.name

    # -------- diagnostics ---------------------------------------------

    def is_available(self) -> tuple[bool, str]:
        cfg = self.config
        if not cfg.ip:
            return False, "no IP configured for this printer"
        if not cfg.access_code:
            return False, "no access code configured (turn on Developer Mode on the X1C and copy the code from the screen)"
        if not cfg.serial:
            return False, "no serial number configured"
        try:
            with socket.create_connection((cfg.ip, 990), timeout=4):
                pass
        except OSError as e:
            return False, f"can't reach {cfg.ip}:990 — {e}. Confirm the printer is on, on the LAN, and developer mode is enabled."
        return True, "reachable"

    def slicer_hint(self) -> dict:
        return {
            "printer_profile": self.config.printer_profile,
            "process_profile": self.config.process_profile,
            "filament_profile": self.config.filament_profile,
        }

    # -------- status (best-effort) ------------------------------------

    def status(self) -> PrinterStatus:
        # Real-time state lives on MQTT. We surface a static status
        # whenever the printer answers FTP — that confirms it's online
        # and reachable. A future improvement subscribes to the MQTT
        # topic and feeds richer state through.
        ok, why = self.is_available()
        if not ok:
            return PrinterStatus(online=False, state="unreachable", message=why)
        return PrinterStatus(
            online=True,
            state="ready",
            message=(
                "Online (LAN). Real-time status (progress / nozzle / bed) "
                "needs the bambulabs-api MQTT client; install it for live updates."
            ),
        )

    # -------- send a print --------------------------------------------

    def send_print(
        self,
        sliced_path: Path,
        *,
        plate_index: int = 1,
        use_ams: bool = False,
        ams_mapping: list[int] | None = None,
    ) -> tuple[bool, str]:
        ok, why = self.is_available()
        if not ok:
            return False, why
        cfg = self.config

        if not sliced_path.exists():
            return False, f"sliced file not found: {sliced_path}"

        upload_name = f"{sliced_path.stem}-{uuid.uuid4().hex[:6]}{sliced_path.suffix}"
        try:
            self._upload_via_ftps(sliced_path, upload_name)
        except Exception as e:
            return False, f"upload to printer failed: {e}"

        # Try the MQTT print-start path. Without bambulabs-api we still
        # leave the file on the SD card so the user can start it from
        # the printer's screen.
        try:
            from bambulabs_api import Printer as BambuLabsApiPrinter  # type: ignore
        except ImportError:
            return True, (
                f"uploaded to printer SD card as {upload_name}. To start "
                "automatically over MQTT, install the optional dependency "
                "`pip install bambulabs-api`. For now, start the print from "
                "the X1C's touchscreen — the file is in the 'model' folder."
            )

        try:
            client = BambuLabsApiPrinter(cfg.ip, cfg.access_code, cfg.serial)
            client.connect()
            client.start_print(
                upload_name,
                plate_index,
                use_ams=use_ams,
                ams_mapping=ams_mapping or [],
            )
            client.disconnect()
        except Exception as e:
            return True, (
                f"uploaded {upload_name} to the printer, but the MQTT "
                f"start command failed ({e}). Start it from the X1C's "
                "touchscreen."
            )
        return True, f"sent {upload_name} to {cfg.name} and started the print."

    def _upload_via_ftps(self, src: Path, dest_name: str) -> None:
        """Upload `src` into the printer's `model/` folder.

        Implicit FTPS on port 990, user 'bblp', password = access code.
        Bambu printers expect uploads under model/; the cache/ folder is
        also accepted but model/ is what the screen lists."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ftp = _ImplicitFTP_TLS(context=ctx)
        ftp.connect(self.config.ip, 990, timeout=15)
        ftp.login("bblp", self.config.access_code)
        ftp.prot_p()
        # Make sure we're in /model. mkd is harmless if it already exists.
        try:
            ftp.cwd("model")
        except ftplib.error_perm:
            ftp.mkd("model")
            ftp.cwd("model")
        with src.open("rb") as fh:
            ftp.storbinary(f"STOR {dest_name}", fh)
        ftp.quit()


# --------------------------------------------------------------------- #
# Factory                                                                #
# --------------------------------------------------------------------- #


def build_printer(kind: str, config: dict) -> Printer:
    if kind == "bambu_x1c":
        return BambuLabPrinter(BambuPrinterConfig(**config))
    raise ValueError(f"unknown printer kind {kind!r} (have: {PRINTER_KINDS})")
