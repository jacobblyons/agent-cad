# Changelog

All notable changes to Agent CAD are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-05-06

First public release. Early but functional — see "Status" in the README.

### License
- Released under **Apache License 2.0** (was GPL v3 during pre-release
  development). Permissive terms with an explicit patent grant — fits
  this project's "small extensions and integrations" goals better than
  strong copyleft. No external contributors at v0.1.0, so no
  re-licensing consent was needed.

### Added
- Claude-driven parametric CAD desktop app (Windows, pywebview + WebView2).
- CADQuery executor, project model with per-object scripts and parameters,
  and a Tweaks panel that re-runs the model on parameter edits.
- 3D viewer (react-three-fiber) with face / edge / vertex picking and
  pinning; pinned entities ride along with the next chat message.
- First-class sketches as separate artifacts the agent grounds in before
  building 3D geometry.
- Project timeline backed by per-turn git commits — click any commit to
  checkout, branch, or diff.
- Reference-import pipeline (STEP / STL / 3MF / glTF) with inspection
  tools, plus an optional Sketchfab integration.
- Optional Playwright browser tool the agent can drive for datasheet /
  reference lookups, with per-call permission prompts.
- 3D-printer print phase (Bambu X1C, LAN mode) — slice via Bambu Studio
  CLI, send to printer over MQTT, live progress + chamber-camera frames.
- Windows installer + launcher scripts under `scripts/` and a GitHub
  Actions workflow that builds a downloadable release zip on tag push.

[0.1.0]: https://github.com/jacobblyons/agent-cad/releases/tag/v0.1.0
