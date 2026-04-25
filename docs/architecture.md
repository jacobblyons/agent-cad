# Agent CAD — architecture

## Process model
Single Python process. `pywebview` opens a window backed by Edge WebView2 (on Windows). Inside the webview runs a React SPA. Communication is via pywebview's `js_api` bridge — direct Python method calls from JS, and `window.evaluate_js()` (called from a worker thread) for server-pushed events such as agent token streaming.

No HTTP server, no localhost socket, no IPC. Everything is in-process Python.

## Module map
```
backend/app/
  main.py          Window bootstrap, dev/prod url switch
  api.py           JsApi class — surface exposed to the webview
  events.py        Push channel: enqueue → worker drains → window.evaluate_js
  cad/
    document.py    Document, Feature dataclasses, save/load (.ccad)
    features.py    Feature kinds + their executors
    executor.py    Replay loop, snapshot LRU, parameter resolution
    tessellate.py  CADQuery → glTF (+ topo id sidecar JSON)
    selectors.py   Topological selector resolution (named tags, queries, fingerprints)
  agent/
    tools.py       Tool definitions (new_sketch, extrude, set_parameter, ...)
    runner.py      Agent SDK driver, streams tokens + tool calls into events.py
```

## Document format (.ccad)
A zip with:
- `model.json` — feature graph + parameter table + head index
- `comments.json` — annotation list (each pinned to a selector)
- `chat.jsonl` — chat thread (one message per line, supports image attachments by path)
- `thumbnails/` — PNG previews per saved head

## Feature graph
The model is an ordered list of features:
```python
Feature {
  id: "f_7"
  kind: "extrude" | "sketch" | "fillet" | "boolean" | ...
  inputs: { ... }            # references other features by id, or {param: "..."} or {value: ...}
  selectors: { ... }         # named picks, never raw OCCT IDs
  created_by: "agent" | "user"
  msg_id: "..."              # provenance
}

Document {
  features: [Feature]
  head: int                  # timeline cursor; only features[0..head] execute
  parameters: { name: value }
  comments: [Comment]
  chat: [Message]
}
```

`head` lets us suppress trailing features without deleting them — that is what makes "rewind, edit, replay" feel right.

## Versioning
- Operation log is the source of truth. Snapshots are cached.
- Snapshot key = hash of (features[0..i], parameter values touched by those features).
- Editing feature `i` invalidates snapshots for `>= i`.
- Branching = copy the array. (Post-MVP.)

## Topological naming
Feature inputs never store raw OCCT IDs. They store **selectors**:
1. Tag (preferred) — `sketch.tag("top")` applied at creation
2. Named query — "largest planar face normal +Z"
3. Geometric fingerprint — centroid + normal + area, matched within tol on replay
When a selector fails to resolve after an upstream edit, the feature gets a "broken reference" badge in the timeline. The user (or Claude) re-picks.

## UI layout (Claude-like)
```
┌────────────────────────────────────────────────────────────┐
│  [ tab1 ] [ tab2 ] [ + ]                          ─ □ ✕   │
├────────────────────┬───────────────────────────────────────┤
│                    │                                       │
│   Chat thread      │           3D viewer                   │
│   (left, ~400px)   │           (r3f canvas)                │
│                    │                                       │
│                    │         [comment pins overlay]        │
│                    │                                       │
│   ── input ──      │                                       │
│   [ paste image ]  ├───────────────────────────────────────┤
│                    │  Timeline   ◄─────●───────►  ▶ Tweaks │
└────────────────────┴───────────────────────────────────────┘
```
