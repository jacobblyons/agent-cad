"""Thin wrapper around the Sketchfab v3 REST API.

Used by the optional sketchfab_* agent tools to look up reference
geometry for the user's design. Auth uses Sketchfab's `Token` scheme
(an API token from https://sketchfab.com/settings/password); we send
it as `Authorization: Token <api_token>` only when calling Sketchfab.

The integration is OFF by default; the user must enable it in Settings
and supply a token. We never make a network call without that explicit
opt-in.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

API_ROOT = "https://api.sketchfab.com/v3"
USER_AGENT = "Agent-CAD/0.1 (https://github.com/jacobblyons/agent-cad)"
HTTP_TIMEOUT = 30.0


def _headers(token: str | None) -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        h["Authorization"] = f"Token {token}"
    return h


@dataclass
class SearchHit:
    uid: str
    name: str
    description: str
    user: str
    license_label: str
    is_downloadable: bool
    view_count: int
    like_count: int
    thumbnail_url: str | None
    viewer_url: str

    def to_json(self) -> dict:
        return {
            "uid": self.uid,
            "name": self.name,
            "description": self.description,
            "author": self.user,
            "license": self.license_label,
            "downloadable": self.is_downloadable,
            "views": self.view_count,
            "likes": self.like_count,
            "thumbnail_url": self.thumbnail_url,
            "viewer_url": self.viewer_url,
        }


def _pick_thumbnail(thumbnails: dict | None, target_w: int = 512) -> str | None:
    """Pick the thumbnail closest to (but not below) target_w pixels wide.

    Sketchfab returns several sizes; we want a mid-resolution one for the
    agent to actually see structure (thumbnails too small are useless).
    """
    if not thumbnails or not isinstance(thumbnails.get("images"), list):
        return None
    images = [
        img for img in thumbnails["images"]
        if isinstance(img, dict) and img.get("url")
    ]
    if not images:
        return None
    # Sort: smallest >= target first, else largest available.
    above = [img for img in images if int(img.get("width", 0)) >= target_w]
    if above:
        above.sort(key=lambda img: int(img.get("width", 0)))
        return above[0]["url"]
    images.sort(key=lambda img: int(img.get("width", 0)), reverse=True)
    return images[0]["url"]


def _hit_from_result(r: dict) -> SearchHit:
    user = ((r.get("user") or {}).get("username")) or "unknown"
    license_label = ((r.get("license") or {}).get("label")) or "unknown"
    return SearchHit(
        uid=str(r.get("uid", "")),
        name=str(r.get("name", "")),
        description=str(r.get("description", "") or "").strip()[:400],
        user=str(user),
        license_label=str(license_label),
        is_downloadable=bool(r.get("isDownloadable", False)),
        view_count=int(r.get("viewCount", 0) or 0),
        like_count=int(r.get("likeCount", 0) or 0),
        thumbnail_url=_pick_thumbnail(r.get("thumbnails")),
        viewer_url=str(r.get("viewerUrl", "")),
    )


def search(query: str, *, count: int = 10, token: str | None = None,
           downloadable_only: bool = False) -> list[SearchHit]:
    params: dict[str, Any] = {
        "type": "models",
        "q": query,
        "count": max(1, min(count, 24)),
    }
    if downloadable_only:
        params["downloadable"] = "true"
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.get(f"{API_ROOT}/search", params=params, headers=_headers(token))
        r.raise_for_status()
        data = r.json()
    return [_hit_from_result(item) for item in data.get("results", [])]


def get_model(uid: str, *, token: str | None = None) -> dict:
    """Full metadata for one model (more fields than search results give)."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.get(
            f"{API_ROOT}/models/{uid}",
            headers=_headers(token),
        )
        r.raise_for_status()
        return r.json()


def fetch_image_bytes(url: str, *, token: str | None = None) -> bytes:
    """Pull a thumbnail / preview image so we can hand it to the agent as
    an inline image content block."""
    # Thumbnails are public so auth isn't required, but we send it
    # consistently so a future authenticated CDN doesn't break us.
    with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
        r = c.get(url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r.content


@dataclass
class DownloadFormat:
    name: str           # "source", "gltf", "usdz"
    url: str
    extension: str      # ".step", ".stp", ".glb", ".zip", ...
    size_bytes: int


def list_download_formats(uid: str, *, token: str) -> list[DownloadFormat]:
    """Hit the download endpoint to get signed URLs for each available
    format. Sketchfab requires auth for this; the URLs typically expire
    in a few minutes so we fetch fresh each time."""
    if not token:
        raise ValueError("a Sketchfab API token is required to list downloads")
    with httpx.Client(timeout=HTTP_TIMEOUT) as c:
        r = c.get(
            f"{API_ROOT}/models/{uid}/download",
            headers=_headers(token),
        )
        if r.status_code == 401:
            raise PermissionError("Sketchfab rejected the API token (401)")
        if r.status_code == 403:
            raise PermissionError("model is not downloadable with this token (403)")
        r.raise_for_status()
        data = r.json()

    out: list[DownloadFormat] = []
    for key, info in (data or {}).items():
        if not isinstance(info, dict):
            continue
        url = info.get("url")
        if not url:
            continue
        # `source` is the originally-uploaded file; format hints at extension.
        # `gltf` / `usdz` are converted previews. We pull the extension off
        # the URL when available, falling back to format hints.
        ext = _guess_ext(info, key)
        out.append(DownloadFormat(
            name=str(key),
            url=str(url),
            extension=ext,
            size_bytes=int(info.get("size") or 0),
        ))
    return out


def _guess_ext(info: dict, key: str) -> str:
    """Best-effort file extension from Sketchfab download metadata."""
    fmt = (info.get("format") or "").lower().strip(".")
    if fmt:
        return f".{fmt}"
    # Fall back to URL inspection.
    url = info.get("url") or ""
    # Strip query params, look at last path segment.
    path = url.split("?", 1)[0]
    last = path.rsplit("/", 1)[-1].lower()
    if "." in last:
        return "." + last.rsplit(".", 1)[-1]
    if key == "gltf":
        return ".glb"
    if key == "usdz":
        return ".usdz"
    return ""


def download_to_path(format_url: str, dest: Path, *, token: str | None = None) -> Path:
    """Stream a Sketchfab download URL to `dest` (created with parents)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream(
        "GET", format_url, headers=_headers(token),
        timeout=httpx.Timeout(connect=15.0, read=120.0, write=120.0, pool=15.0),
        follow_redirects=True,
    ) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                if chunk:
                    f.write(chunk)
    return dest


# --- helpers exposed for the agent tool layer ---------------------------

# Mirrors `_import_loader.SUPPORTED_EXTS`: every format the project's
# import pipeline knows how to ingest. STEP/IGES/BREP all give full
# boolean + measurement support; STL is mesh-only (acceptable for visual
# reference, not for engineering booleans).
SUPPORTED_DOWNLOAD_EXTS = {
    ".step", ".stp", ".iges", ".igs", ".brep", ".brp",
    ".stl", ".3mf", ".glb", ".gltf",
}
# Preference order: prefer B-rep (best boolean support) over mesh, with
# 3MF before raw STL (richer metadata, same triangulation), and binary
# glTF before text glTF (no separate bin file to chase).
DOWNLOAD_PREFERENCE = [
    ".step", ".stp",
    ".iges", ".igs",
    ".brep", ".brp",
    ".3mf",
    ".stl",
    ".glb", ".gltf",
]


def find_importable_format(
    formats: list[DownloadFormat],
) -> DownloadFormat | None:
    """Pick the best download format we can actually import — preferring
    B-rep formats (STEP > IGES > BREP) over mesh (STL).

    Sketchfab sometimes packages source files inside a zip; we don't
    unwrap zips here. If none of the offered formats are directly
    importable, return None and let the caller surface a clear message.
    """
    by_ext: dict[str, DownloadFormat] = {}
    for f in formats:
        ext = f.extension.lower()
        if ext in SUPPORTED_DOWNLOAD_EXTS and ext not in by_ext:
            by_ext[ext] = f
    for ext in DOWNLOAD_PREFERENCE:
        if ext in by_ext:
            return by_ext[ext]
    return None


# Back-compat alias for any callers that still reach for the old name.
find_step_format = find_importable_format


def safe_filename_from_name(name: str) -> str:
    """Sanitize a model name for use as an import filename stem. Mirrors
    Project.sanitize_object_name's character set so the import sits
    cleanly alongside other artifacts."""
    keep = []
    for ch in name.strip():
        if ch.isalnum() or ch in ("_", "-"):
            keep.append(ch)
        elif ch.isspace():
            keep.append("-")
    out = "".join(keep).strip("-_")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "sketchfab-model"


# Re-export shutil so tools.py can use it for any post-download cleanup
# without re-importing inside hot paths.
_shutil = shutil
