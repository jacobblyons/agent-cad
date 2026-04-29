"""Printer drivers — send a sliced job to a physical machine and report status.

A `Printer` knows how to upload a sliced file (typically a .3mf or
.gcode) to one specific 3D printer and start a print. Implementations
are vendor-specific; abstraction lets us add OctoPrint, Klipper /
Moonraker, PrusaLink, etc. later without touching the UI.

Current implementations:
    - BambuLabPrinter — talks to a Bambu X1/X1C/P1S/A1 in LAN /
      developer mode using FTPS upload + MQTT print start. The MQTT
      print-start payload is documented at
      https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md.
"""
from __future__ import annotations

import ftplib
import json
import socket
import ssl
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

PRINTER_KINDS = ["bambu_x1c"]


# Bambu's MQTT bed_type strings → slicer's curr_bed_type values.
# We keep the slicer-side names in the canonical Bambu Studio form so
# they pass straight through into the override JSON.
_BAMBU_BED_TYPE_MAP: dict[str, str] = {
    "textured_plate":        "Textured PEI Plate",
    "cool_plate":            "Cool Plate",
    "hot_plate":             "High Temperature Plate",
    "eng_plate":             "Engineering Plate",
    "supertack_plate":       "Bambu Cool Plate SuperTack",
    # Variants reported by some firmware revisions; map to the closest.
    "textured_pei_plate":    "Textured PEI Plate",
    "cool_plate_smooth":     "Cool Plate",
}


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


@dataclass
class FilamentSlot:
    """One AMS tray's filament — or, on a printer with no AMS, the
    single externally-spooled filament."""
    tray_id: int                 # 0..n for AMS slots; 254 for external spool
    type: str = ""               # "PLA" / "PETG" / "ABS" / "ASA" / "PC" / ...
    sub_brand: str = ""          # "Bambu PLA Basic" / "Generic PETG" / etc.
    color_hex: str = ""          # 6- or 8-char RGBA hex, e.g. "FFFFFFFF"
    tray_info_idx: str = ""      # Bambu's internal profile id (eg "GFA00")

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class PrinterState:
    """Live snapshot of what the printer reports about itself.

    Populated by `BambuLabPrinter.get_state()`. Every field is best-
    effort: when the printer's report doesn't include something we
    leave it empty / None rather than guessing.
    """
    online: bool
    bed_type: str = ""           # Bambu's mqtt name (eg 'textured_plate'); "" if unknown
    bed_type_slicer: str = ""    # Translated to slicer's `curr_bed_type` value
    active_tray: int = -1        # Currently-loaded AMS slot, 254 = external spool, -1 = none
    slots: list[FilamentSlot] = field(default_factory=list)
    nozzle_diameter_mm: float | None = None
    nozzle_type: str = ""        # "stainless_steel" / "hardened_steel" / ...
    error: str = ""              # If the query failed, what went wrong

    def to_json(self) -> dict:
        return {
            **asdict(self),
            "slots": [s.to_json() for s in self.slots],
        }

    def active_slot(self) -> FilamentSlot | None:
        """The AMS slot currently feeding the nozzle, or None if no
        printer-side selection / no slots reported."""
        if self.active_tray < 0:
            return None
        for s in self.slots:
            if s.tray_id == self.active_tray:
                return s
        # External spool has tray_id 254; if the printer reports
        # active_tray=254 with no matching slot we still want to surface
        # whatever single slot is reported.
        if self.slots and self.active_tray == 254:
            return self.slots[0]
        return None


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
    # Fallback build plate when the printer's MQTT report doesn't say
    # which plate is installed (current X1C firmware doesn't expose
    # this — only the GUI knows). Slicer-side names: "Cool Plate",
    # "Engineering Plate", "High Temperature Plate", "Textured PEI Plate",
    # "Bambu Cool Plate SuperTack".
    default_bed_type: str = "Textured PEI Plate"

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
    """ftplib FTP_TLS adapted for Bambu's LAN FTPS server.

    Two patches over stdlib's FTP_TLS:

      1. Implicit TLS: Bambu's FTPS listens on port 990 with TLS
         negotiated *before* the first FTP command. stdlib only knows
         explicit-mode (AUTH TLS after a plaintext connect), so we wrap
         the control socket in TLS the moment ftplib assigns it (Python
         issue #31662 — the canonical workaround).

      2. SSL session resumption on the data connection: Bambu's vsftpd
         requires the data-channel TLS session to be identical to the
         control-channel session — a defence against data-connection
         hijacking. Python's ftplib doesn't do this; data uploads
         instead die mid-handshake with
         `EOF occurred in violation of protocol (_ssl.c:2406)` during
         STOR. We override `ntransfercmd` to pass `session=` from the
         control socket to the data socket's `wrap_socket` call.
    """

    @property
    def sock(self) -> socket.socket | None:  # type: ignore[override]
        return self._sock

    @sock.setter
    def sock(self, value: socket.socket | None) -> None:
        if value is not None and self.context and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):  # type: ignore[override]
        # Get the raw data socket via the base FTP class, then wrap it
        # ourselves so we can pass `session=` for resumption — without
        # which Bambu's vsftpd kills the connection.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            sock = self._sock
            session = sock.session if isinstance(sock, ssl.SSLSocket) else None
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=session,
            )
        return conn, size


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
        # Serial number is intentionally NOT required here — uploads only
        # need IP + access code (FTPS auth as `bblp`). The serial is only
        # used by the MQTT print-start path, which we degrade gracefully
        # when missing (file lands on SD card, user taps it on the screen).
        cfg = self.config
        if not cfg.ip:
            return False, "no IP configured for this printer"
        if not cfg.access_code:
            return False, "no access code configured (turn on Developer Mode on the X1C and copy the code from the screen)"
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

    # -------- live state query (MQTT pushall) -------------------------

    def get_state(self, *, timeout: float = 5.0) -> PrinterState:
        """Ask the printer for a full status dump and parse it.

        Subscribes to `device/<sn>/report`, publishes a `pushing/pushall`
        request on `device/<sn>/request`, and merges every report
        message that arrives during the timeout window. Bambu firmware
        often splits the dump across several messages (one with `print`
        info, another with `info`/`hms`, etc.), so we accumulate until
        either we have everything we need or the timeout expires.

        The returned `PrinterState` always has `online` set; missing
        fields stay empty / None when the printer didn't include them.
        """
        cfg = self.config
        if not cfg.serial:
            return PrinterState(online=False, error="no serial configured")
        if not cfg.access_code:
            return PrinterState(online=False, error="no access code configured")

        try:
            import paho.mqtt.client as mqtt  # noqa: PLC0415
        except ImportError as e:
            return PrinterState(online=False, error=f"paho-mqtt not installed: {e}")

        merged: dict = {}
        ready = threading.Event()

        def on_message(_client, _userdata, msg):
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return
            # Bambu sends partial deltas as well as full dumps. We deep-
            # merge each into a single rolling snapshot so the parser
            # always sees the fullest picture available.
            _deep_merge(merged, payload)
            # We have what we need once `print.bed_type` is set AND
            # either `print.ams.ams[]` is populated OR the report
            # explicitly says no AMS is installed (`tray_now` set, no
            # `ams.ams` key — single external spool).
            print_node = merged.get("print") or {}
            if print_node.get("bed_type"):
                ams_node = print_node.get("ams") or {}
                if ams_node.get("ams") is not None or "tray_now" in ams_node:
                    ready.set()

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"agent-cad-q-{uuid.uuid4().hex[:8]}",
        )
        client.username_pw_set("bblp", cfg.access_code)
        client.tls_set_context(ctx)
        client.tls_insecure_set(True)
        client.on_message = on_message

        try:
            client.connect(cfg.ip, 8883, keepalive=30)
        except OSError as e:
            return PrinterState(
                online=False,
                error=f"MQTT connect to {cfg.ip}:8883 failed: {e}",
            )

        report_topic = f"device/{cfg.serial}/report"
        request_topic = f"device/{cfg.serial}/request"

        client.loop_start()
        try:
            client.subscribe(report_topic, qos=1)
            # Force a full snapshot rather than waiting for the next
            # periodic broadcast. The pushall response lands on the
            # report topic we just subscribed to.
            client.publish(
                request_topic,
                json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}),
                qos=1,
            )
            ready.wait(timeout)
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except OSError:
                pass

        if not merged:
            return PrinterState(
                online=False,
                error=(
                    f"no MQTT messages from {cfg.ip} within {timeout:.0f}s — "
                    "is Developer Mode / LAN Mode actually enabled, and is "
                    "the access code correct?"
                ),
            )
        return _parse_printer_state(merged)

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

        # MQTT print-start needs the printer's serial (the topic includes
        # it: `device/<serial>/request`). Without a serial we can still
        # upload — the user just taps the file on the touchscreen to start.
        if not cfg.serial:
            return True, (
                f"uploaded to printer SD card as {upload_name}. No serial "
                "number is configured, so I can't auto-start the print over "
                "MQTT — tap the file in the 'model' folder on the X1C's "
                "touchscreen to begin. (Add the serial in Settings to enable "
                "one-click send-and-start.)"
            )

        try:
            self._mqtt_start_print(
                upload_name, plate_index=plate_index,
                use_ams=use_ams, ams_mapping=ams_mapping or [],
            )
        except Exception as e:
            return True, (
                f"uploaded {upload_name} to the printer, but the MQTT "
                f"start command failed ({e}). Start it from the X1C's "
                "touchscreen — the file is in the 'model' folder."
            )
        return True, f"sent {upload_name} to {cfg.name} and started the print."

    def _mqtt_start_print(
        self,
        upload_name: str,
        *,
        plate_index: int,
        use_ams: bool,
        ams_mapping: list[int],
    ) -> None:
        """Publish a `project_file` command on the printer's MQTT bus to
        start printing the file we just uploaded.

        Payload shape mirrors what Bambu Studio's GUI sends — documented
        in the OpenBambuAPI reverse-engineering project
        (github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md). The printer
        responds on `device/<serial>/report` with progress events; we
        don't subscribe to those here, just fire-and-forget the start
        command.
        """
        # Local import so the rest of the slicer / printer surface still
        # imports cleanly when paho-mqtt isn't yet installed (e.g. fresh
        # checkout before `pip install -e .`).
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        cfg = self.config
        topic = f"device/{cfg.serial}/request"
        payload = {
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                # Bambu's plate-i gcode lives at this path inside the 3MF
                # archive; the firmware reads it from the SD card image
                # we uploaded under /model/.
                "param": f"Metadata/plate_{plate_index}.gcode",
                "subtask_name": upload_name,
                "url": f"file:///mnt/sdcard/model/{upload_name}",
                "bed_type": "auto",
                "timelapse": False,
                "bed_leveling": True,
                "flow_cali": False,
                "vibration_cali": True,
                "layer_inspect": False,
                "use_ams": bool(use_ams),
                "ams_mapping": list(ams_mapping or []),
                # Free-form ids — Bambu's GUI fills these from cloud
                # records, but the LAN firmware doesn't validate them.
                "profile_id": "0",
                "project_id": "0",
                "subtask_id": "0",
                "task_id": "0",
            }
        }

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"agent-cad-{uuid.uuid4().hex[:8]}",
        )
        client.username_pw_set("bblp", cfg.access_code)
        client.tls_set_context(ctx)
        # Bambu accepts insecure TLS (cert is self-signed). Cleaner than
        # disabling cert checks via ssl.SSLContext.
        client.tls_insecure_set(True)

        try:
            client.connect(cfg.ip, 8883, keepalive=30)
        except OSError as e:
            raise RuntimeError(
                f"MQTT connect to {cfg.ip}:8883 failed: {e}. "
                "Confirm the X1C is in LAN-only or Developer Mode."
            ) from e

        client.loop_start()
        try:
            info = client.publish(topic, json.dumps(payload), qos=1)
            # qos=1 with wait_for_publish guarantees the broker received
            # the command before we drop the connection — without it the
            # client side can disconnect before the bytes leave the
            # socket and the printer never sees the start.
            info.wait_for_publish(timeout=10)
            if not info.is_published():
                raise RuntimeError("MQTT publish timed out (printer didn't ack)")
            # Small grace period so the printer can ingest the message
            # before we tear the TLS session down.
            time.sleep(0.3)
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except OSError:
                pass

    def _upload_via_ftps(self, src: Path, dest_name: str) -> None:
        """Upload `src` into the printer's `model/` folder.

        Implicit FTPS on port 990, user 'bblp', password = access code.
        Bambu printers expect uploads under model/; the cache/ folder is
        also accepted but model/ is what the screen lists.

        Each FTP step raises a more specific exception so the caller can
        report exactly where things broke (connect vs login vs STOR)
        instead of swallowing every failure as a generic upload error.
        """
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ftp = _ImplicitFTP_TLS(context=ctx)
        try:
            ftp.connect(self.config.ip, 990, timeout=15)
        except (OSError, ssl.SSLError) as e:
            raise RuntimeError(f"connect to {self.config.ip}:990 failed: {e}") from e
        try:
            ftp.login("bblp", self.config.access_code)
        except ftplib.error_perm as e:
            raise RuntimeError(
                f"login refused — wrong access code? Check Settings → WLAN → "
                f"ⓘ on the X1C and update Settings → Printers in this app. ({e})"
            ) from e
        # PROT_P switches the data connection to TLS — required because
        # Bambu's plaintext data port is closed.
        ftp.prot_p()
        # Make sure we're in /model. mkd is harmless if it already exists.
        try:
            ftp.cwd("model")
        except ftplib.error_perm:
            ftp.mkd("model")
            ftp.cwd("model")
        try:
            with src.open("rb") as fh:
                ftp.storbinary(f"STOR {dest_name}", fh)
        except (ftplib.error_perm, ftplib.error_temp, ssl.SSLError, OSError) as e:
            raise RuntimeError(
                f"STOR {dest_name} failed: {e}. The connection succeeded but "
                "the upload itself was rejected — usually disk-full, a "
                "filename Bambu doesn't like, or a TLS hiccup mid-stream."
            ) from e
        try:
            ftp.quit()
        except (ftplib.error_temp, ssl.SSLError, OSError):
            # Bambu sometimes drops the control connection on QUIT; the
            # file is already uploaded by this point so we don't care.
            pass


# --------------------------------------------------------------------- #
# MQTT report parsing helpers                                            #
# --------------------------------------------------------------------- #


def _deep_merge(dst: dict, src: dict) -> None:
    """Recursively merge `src` into `dst`. Lists in `src` replace lists
    in `dst` (Bambu sends full lists when AMS state changes — partial
    updates would be ambiguous about whether a missing slot was
    deliberately removed or just unchanged)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


def _parse_printer_state(merged: dict) -> PrinterState:
    """Translate a merged Bambu MQTT report into our PrinterState.

    The report shape varies a bit across firmware revs but the keys we
    care about have been stable for a while. Anything missing → blank
    field, never an exception."""
    print_node = (merged.get("print") or {}) if isinstance(merged.get("print"), dict) else {}
    bed_type = str(print_node.get("bed_type") or "")
    bed_type_slicer = _BAMBU_BED_TYPE_MAP.get(bed_type, "")

    ams_root = print_node.get("ams") or {}
    if not isinstance(ams_root, dict):
        ams_root = {}

    # `tray_now` is a string like "0".."15" or "254" (external spool).
    try:
        active_tray = int(ams_root.get("tray_now", "-1"))
    except (TypeError, ValueError):
        active_tray = -1

    slots: list[FilamentSlot] = []
    ams_units = ams_root.get("ams") or []
    if isinstance(ams_units, list):
        for unit in ams_units:
            if not isinstance(unit, dict):
                continue
            unit_id = unit.get("id")
            try:
                unit_offset = int(unit_id) * 4 if unit_id is not None else 0
            except (TypeError, ValueError):
                unit_offset = 0
            for tray in unit.get("tray") or []:
                if not isinstance(tray, dict):
                    continue
                # Empty slots have empty `tray_type`.
                ttype = str(tray.get("tray_type") or "")
                if not ttype:
                    continue
                try:
                    local_id = int(tray.get("id", "0"))
                except (TypeError, ValueError):
                    local_id = 0
                slots.append(FilamentSlot(
                    tray_id=unit_offset + local_id,
                    type=ttype.upper(),
                    sub_brand=str(tray.get("tray_sub_brands") or ""),
                    color_hex=str(tray.get("tray_color") or ""),
                    tray_info_idx=str(tray.get("tray_info_idx") or ""),
                ))

    # Printer with no AMS — the active spool is reported via the
    # `vt_tray` node (virtual tray, id=255). Bambu puts this at the
    # `print.vt_tray` level, NOT under `print.ams`. Some firmware revs
    # also stuff a copy under `ams.vt_tray`, so we check both spots.
    if not slots:
        vt = print_node.get("vt_tray") or ams_root.get("vt_tray")
        if isinstance(vt, dict) and vt.get("tray_type"):
            slots.append(FilamentSlot(
                tray_id=254,
                type=str(vt.get("tray_type") or "").upper(),
                sub_brand=str(vt.get("tray_sub_brands") or ""),
                color_hex=str(vt.get("tray_color") or ""),
                tray_info_idx=str(vt.get("tray_info_idx") or ""),
            ))

    # Nozzle metadata. `nozzle_diameter` is a stringified float; older
    # firmware called it `nozzle_diameter_value`.
    nozzle_diam_raw = (
        print_node.get("nozzle_diameter")
        or print_node.get("nozzle_diameter_value")
    )
    try:
        nozzle_diameter_mm = float(nozzle_diam_raw) if nozzle_diam_raw else None
    except (TypeError, ValueError):
        nozzle_diameter_mm = None

    return PrinterState(
        online=True,
        bed_type=bed_type,
        bed_type_slicer=bed_type_slicer,
        active_tray=active_tray,
        slots=slots,
        nozzle_diameter_mm=nozzle_diameter_mm,
        nozzle_type=str(print_node.get("nozzle_type") or ""),
    )


# --------------------------------------------------------------------- #
# Factory                                                                #
# --------------------------------------------------------------------- #


def build_printer(kind: str, config: dict) -> Printer:
    if kind == "bambu_x1c":
        return BambuLabPrinter(BambuPrinterConfig(**config))
    raise ValueError(f"unknown printer kind {kind!r} (have: {PRINTER_KINDS})")
