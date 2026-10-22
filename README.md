# Local Asset Studio · v0.3.0

**Local text-to-image-to-3D asset production, with a browser workbench and human approval.**

Design a reference, choose what to build, run a persistent production queue, and review the resulting 3D model in your browser. Inference runs on your own machine through ComfyUI, TRELLIS.2 and Ollama. Other computers on the same trusted LAN can use the web interface.

> **A CUDA-capable NVIDIA GPU with at least 24 GB of dedicated VRAM is required for the supported full pipeline.** The development machine uses an RTX 3090 24 GB on Windows 11. Lower-VRAM and CPU-only end-to-end production are not validated or supported configurations. The VLM step requires approximately 19,000 MiB of free VRAM; close other GPU-heavy applications.

[Türkçe README](README.tr.md) · [Installation](docs/INSTALLATION.md) · [Usage](docs/USAGE.md) · [Architecture](docs/ARCHITECTURE.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)


**TRELLIS-only release:** Hunyuan generation and its installers have been removed. Existing Hunyuan job records are never silently converted; create a new TRELLIS job from a selected reference. No private game assets, prompts or catalogs are bundled.

The included native wheels target **Windows x64, Python 3.12, PyTorch 2.10/CUDA 13.0 and sm_86 (RTX 3090)**. Other GPU architectures need a matching native build and are not certified by this release. DINOv3 requires a free Hugging Face account, accepted terms and approved access; setup cannot bypass this approval. [Migration and limitations](docs/TRELLIS_MIGRATION.md).

## What it does

- Create new assets from Turkish/English prompts or your own optional catalog.
- Iterate on reference designs through prompt and component changes; select references for 3D production.
- Run manual 3D selections and review-driven regenerations in one persistent, reorderable queue.
- See the active asset, actual stage, measured stage time, pending items, and failure reasons.
- Remove waiting work or pause after the current job; jobs continue when the browser is closed.
- Generate shape, optimize polygon counts, generate PBR, process meshes in Blender, and export delivery files.
- Rotate, zoom, inspect wireframe/clay views, accept, request revisions, or reject models in the browser.
- Treat human model acceptance independently from technical validation. Raw geometry or a technically blocked model can be accepted without fabricating a PASS or a completed delivery package.
- Preserve old outputs and link each revision to its source model and review note. A review creates at most one automatic regeneration; failed work is not retried indefinitely.

```mermaid
flowchart LR
  A[Prompt / catalog] --> B[Reference image]
  B --> C[Human selects reference]
  C --> D[Persistent queue]
  D --> E[TRELLIS Shape]
  E --> F[Geometry check / optimization]
  F --> G[PBR / Blender / export]
  G --> H[Technical and visual reports]
  H --> I[Browser 3D review]
  I --> J[Accept model]
  I --> K[Revision / rejection note]
  K --> L[New reference with requested changes]
  L --> D
```

Ordinary references require human selection. Revision/rejection notes explicitly authorize a new candidate to proceed automatically. Reference semantic gates are bypassed in the web human-selection flow; file/mask integrity, GPU ownership, downstream geometry checks, technical validation and final reports remain active. CLI workflows retain their own policies.

## Stack and hardware

| Component | Configuration used in development |
|---|---|
| Operating system | Windows 11; other platforms are not validated |
| GPU | **NVIDIA RTX 3090, 24 GB VRAM — 24 GB minimum for the supported full workflow** |
| System memory | Development host has approximately 48 GB RAM |
| Core runtime | Python 3.12.11, PyTorch 2.6.0 + CUDA 12.6 |
| Geometry / texture | TRELLIS.2 through an isolated PyTorch runtime |
| Reference providers | Z-Image-Turbo BF16, Qwen-Image-2512 FP8, DreamShaper 8 |
| Visual evaluation | Ollama; configured local tag `qwen3.6:27b` |
| Prompt / revision translation | Local `qwen3:4b`, CPU-only |
| Mesh processing | Blender 5.2.2 LTS, PyMeshLab, trimesh |
| Workbench / queue | Flask, SQLite, local Three.js viewer |

GPU work is serialized. Shape, image generation, paint and VLM models are not intended to remain resident simultaneously. Model weights and Python/ComfyUI environments are large and are **not included** in this repository.

The image runtimes and isolated TRELLIS runtime use separate environments. The installer prepares ComfyUI v0.18.5 with PyTorch 2.10.0 + CUDA 13.0 in its own environment; do not install its requirements into the TRELLIS environment.

## One-click setup

Download [**Install-LocalAssetStudio.bat**](https://github.com/CakarK/localassetstudio/releases/download/v0.3.0/Install-LocalAssetStudio.bat) from the v0.3.0 GitHub release and double-click it. It downloads the versioned source and runs setup, then opens the studio. A downloaded source ZIP includes the same installer; double-click `Start-LocalAssetStudio.bat` afterwards to start, or to install if missing.

The installer verifies free DINOv3 account access before large model downloads. If terms/login are needed, it opens the model page and prompts for local login; only you can accept the provider terms. Rerun the same BAT after approval. An older installation in the default destination is preserved and the new release uses a versioned sibling folder instead of running the old Hunyuan installer.

## Getting started

On **Windows 11 x64**, open PowerShell and run this single command. Git and Python do not need to be installed first:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/CakarK/localassetstudio/main/install.ps1 | iex"
```

The bootstrap downloads this repository into `%LOCALAPPDATA%\LocalAssetStudio`. The full installer prepares portable Git/uv/Blender/Ollama, Python 3.12.11, separate core-image, modern-image and TRELLIS environments, all three supported image providers, TRELLIS/DINOv3/BiRefNet/U2Net weights, and the two Ollama models. It records local paths, checks CUDA/extensions/Blender, starts the services and opens the studio. **No asset or model inference is submitted by setup.**

Expect approximately **84.8 GB (79.0 GiB) of model downloads**, plus runtime/package downloads; reserve **120 GiB free disk** for a fresh full install. A suitable NVIDIA driver and **24 GB VRAM** are prerequisites. Upstream model/runtime licenses apply: review [the notices](THIRD_PARTY_NOTICES.md). Network, disk and model-access failures stop with a log; rerun the same command to resume verified files. Existing incompatible source/model versions are preserved and reported rather than replaced.

Already cloned the repository? Use:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-LocalAssetStudio.ps1
# Show the download/installation plan without changing anything:
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-LocalAssetStudio.ps1 -Plan
```

On later launches, use `Start-LocalAssetStudio.ps1` to start all services together. See [installation and recovery](docs/INSTALLATION.md) for a custom drive, service conflicts and no-start mode.

**Verification boundary:** installer resume/hash/preservation tests, Windows PowerShell checks and dependency resolution are validated. A complete fresh-machine GPU installation and end-to-end inference with every newly pinned public component have not been certified. Setup performs real smoke checks on the destination machine and only reports success if they pass.

| Service / page | Default address |
|---|---|
| Asset workbench | http://localhost:8088/ |
| Live production queue | http://localhost:8088/queue |
| 3D model review | http://localhost:8088/reviews |
| Classic image ComfyUI | http://127.0.0.1:8190 |
| Image ComfyUI | http://127.0.0.1:8191 |
| Ollama | http://127.0.0.1:11434 |

The studio generates a local access code in `AI3D/State/web-access-code.txt`. For another computer on the same LAN, open `http://<server-LAN-IP>:8088`. `Enable-AssetStudioLAN.ps1` can create the private-network firewall rule. Keep the inference services on localhost. This workbench is intended for a trusted LAN; internet hosting, TLS and multi-user identity management are not included.

## Production and review

1. Create an asset or select an entry from your own catalog. Describe the reference you want.
2. Generate a reference and change the prompt as needed. Ordinary reference generation is not a background image queue.
3. Select suitable references, add them to 3D production, and start the queue.
4. Use `/queue` to see names, notes, actual stages and failures. Reorder or remove waiting work there.
5. Open `/reviews` to inspect the GLB and record your decision. Technical failures and VLM scores are informational for your acceptance decision; missing/changed files and active mutation still prevent an unsafe/stale decision.
6. A revision or rejection note creates a new candidate using the existing generation pipeline. It does not perform exact local surgery on the old mesh. The previous output remains intact.

The default asset policy requests **object geometry only**: no terrain tile, ground patch, base plate, plinth or pedestal, including for buildings and trees. Generative models can still violate this request; downstream reports and human review remain necessary.

## Outputs and limits

Successful deliveries can contain GLB, FBX, OBJ, Blender files, albedo/roughness/metallic maps, LOD exports, PNG previews and JSON reports. The exact contents depend on which stages completed. Normal/AO maps are not claimed when absent. A raw accepted model remains technically incomplete until its remaining work has actually succeeded.

Generated data stays under `AI3D/Outputs`; SQLite state, logs and quality reports are local-only. Generated assets, model weights, machine credentials, development databases and Unreal pilot projects are excluded from this repository. No assets are automatically imported into your Unreal project.

Automatic decimation/UV work is not manual retopology. Fine details, exact object identity, requested changes and LOD appearance require inspection. Human acceptance never rewrites an AI or technical failure into a PASS. A previously tested isolated Unreal import does not guarantee compatibility with every engine/project configuration.

## Development

Run the existing tests with an environment containing the locked Python dependencies:

```powershell
.\AI3D\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Source-release verification is recorded in [installation validation](docs/INSTALLATION_VALIDATION.md). No private evidence files or external runtime source are bundled.

Tests use isolated fixtures and mocked inference; passing them does not prove that your GPU models, Blender or ComfyUI installation are configured. Do not run integration tests that acquire the global production lock while real production is active. See [CONTRIBUTING.md](CONTRIBUTING.md).

The implementation keeps the legacy `ai3d_manager` module and launcher names for compatibility. The project and web application are named **Local Asset Studio**.

The public distribution contains **no game catalog, game prompts, shared art direction, character names or game grid settings**. It opens in custom-prompt mode with an empty catalog. Enter your own optional style and negative prompt in the web form. An optional JSON catalog can be selected with `LAS_CATALOG_PATH`; see [catalog configuration](docs/INSTALLATION.md#your-own-catalog).
