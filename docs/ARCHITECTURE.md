# Architecture

Local Asset Studio orchestrates existing local inference services; it does not include model weights or replace ComfyUI/Blender.

| Area | Source |
|---|---|
| HTTP session, CSRF, API, static pages | `ai3d_manager/web_app.py` |
| Staged web worker and exact-file authorization | `web_control.py` |
| Persistent 3D selection queue | `web_queue.py` |
| Unified visible/dispatch order | `web_production.py` |
| Review notes, lineage and bounded regeneration | `web_revisions.py`, `web_design.py` |
| Human model decisions and immutable file identity | `web_reviews.py` |
| Production jobs, attempts and stage checkpoints | `database.py`, `worker.py` |
| Image providers / GPU coordination | `image_providers.py`, `quality_vlm.py` |
| Geometry/reference analysis and quality reports | `quality_gates.py`, `quality_service.py`, `quality_store.py` |
| Catalog parsing | `catalog.py` |
| CLI entry point | `asset_manager.py`, `cli.py` |
| Local ComfyUI adapters / Blender automation | `AI3D/Scripts/` |
| Browser workbench, queue, 3D viewer | `ai3d_manager/web_static/` |

SQLite stores persistent job/attempt state and a distinct web control/review history. Completed artifacts are bound to size/SHA256 metadata. Human approval is recorded for exact model bytes; it is not a technical PASS. Partial human acceptance preserves the real production cursor/error and cannot authorize unrequested inference.

The unified board and scheduler use the same candidate list and persisted order. A single active web worker and existing global GPU ownership checks serialize GPU work. Uncertain remote submissions/residency are not resolved by silently launching a new request or unloading a foreign model.

Ports 8190/8191 serve the separate core/image ComfyUI environments, 11434 serves Ollama, and 8088 serves the LAN workbench. They share a physical GPU but not a Python environment. Source/module and launcher identifiers inherited from the earlier implementation remain where compatibility requires them.
