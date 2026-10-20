# Installation

## One command on Windows 11

A **24 GB VRAM NVIDIA GPU**, a working NVIDIA driver and **180 GiB free space on the destination drive** are required for a fresh full installation. The image runtime uses CUDA 13.0; the Hunyuan runtime uses CUDA 12.6. Install a driver compatible with both. The installer checks GPU 0 and runs a tiny CUDA tensor check in each environment; it does not install/update GPU drivers or run model inference.

Open **64-bit PowerShell** and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/CakarK/localassetstudio/main/install.ps1 | iex"
```

The command executes the bootstrap from this repository. You can [inspect its source](../install.ps1) before running it. It downloads one resolved repository commit through HTTPS. The pinned external archives and model weights are then checked against their sizes and SHA256 values in [install.lock.json](../AI3D/install.lock.json). ExecutionPolicy Bypass applies to these child PowerShell processes, not your permanent machine policy.

The default location is `%LOCALAPPDATA%\LocalAssetStudio`. No Git, Python, Blender or Ollama installation is required beforehand. The portable tools live under `AI3D/Tools`; managed Python, package caches, models and outputs remain on the selected installation drive. No system Python environments or shell profiles are edited. Administrator rights are not required for these portable tools; optional LAN firewall changes are separate.

For another drive, this is also a single PowerShell command:

```powershell
& ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/CakarK/localassetstudio/main/install.ps1'))) -InstallDirectory 'D:\LocalAssetStudio'
```

An existing nonempty unrelated directory is refused. A repeated bootstrap uses an existing installation instead of replacing its source files. It is a setup/resume command, not a destructive source updater. For an existing Git clone, pull code updates with Git and run the local installer again after stopping that installation's ComfyUI services.

## What gets installed

| Part | Pinned setup |
|---|---|
| Python / manager | Python 3.12.11, existing `AI3D/requirements.lock.txt` |
| Hunyuan ComfyUI | v0.3.59 and integration revisions from `AI3D/sources.lock.json` |
| Core CUDA | PyTorch 2.6.0 + CUDA 12.6; Windows cp312 rasterizer/inpaint wheels |
| Native image ComfyUI | v0.18.5, separate `AI3D/ImageComfyUI/.venv` |
| Image CUDA | PyTorch 2.10.0 + CUDA 13.0; `AI3D/image-requirements.lock.txt` |
| Images | DreamShaper 8, Qwen-Image-2512 FP8, Z-Image-Turbo BF16 |
| Shape / textures | Hunyuan3D 2.1 shape/VAE/PBR, DINOv2 |
| Background removal | Local U2Net ONNX, CPU |
| Mesh / export | Portable Blender 5.0.1 |
| Prompt composition | Ollama `qwen3:4b`, requests use CPU |
| Visual evaluation | Public Ollama `qwen3.6:27b`, exact manifest digest pinned |

Model downloads total approximately **92.79 GB (86.42 GiB)**. Python wheels, source trees and tool archives add to that. The fresh-install free-space budget includes caches/extraction room; reruns credit already present model files and environments. Upstream access restrictions can still require a user-owned Hugging Face token/accepted terms. No token is stored in this repository or fabricated by setup. Review [upstream licenses](../THIRD_PARTY_NOTICES.md).

The public Qwen tag now resolves to digest `bcbdbd4bcf86f2c4a56cd1698fb31f9434f7dd99752f71b636c750aaa67a49a5`. The older development-machine digest is not reproducible through that public tag. The public quality profile and installer agree on the new explicit pin; the identity check is preserved. This newer public revision has not been given the old revision's inference/evaluation evidence. If a public tag changes later, setup stops instead of silently accepting a different model.

DreamShaper's original registry entry pointed at a Diffusers-only repository revision. The installer uses `Lykon/DreamShaper/DreamShaper_8_pruned.safetensors` at the pinned revision with the **same checkpoint SHA256** and local filename; the inference workflow/checkpoint remains unchanged.

## Local clone, plan and launch

From an already cloned repository:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-LocalAssetStudio.ps1
# Read-only plan; no downloads, environment changes or services:
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-LocalAssetStudio.ps1 -Plan
# Prepare and verify files/environments without starting the ComfyUI/web services:
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-LocalAssetStudio.ps1 -NoStart
# Later, start all services with one command:
powershell -NoProfile -ExecutionPolicy Bypass -File .\Start-LocalAssetStudio.ps1
```

`-NoStart` still starts the local Ollama service if needed to prepare its models. `-NoBrowser` suppresses the browser opening. Setup never submits image, VLM, shape or PBR inference. CUDA smoke checks allocate only a tiny test tensor and release it on process exit. `AI3D/State/install-runtime.json` records this installation's portable paths, including Blender; existing explicit Blender setting overrides still take precedence.

| Service / page | Default address |
|---|---|
| Studio | http://localhost:8088/ |
| Production queue | http://localhost:8088/queue |
| 3D review | http://localhost:8088/reviews |
| Hunyuan ComfyUI | http://127.0.0.1:8190 |
| Image ComfyUI | http://127.0.0.1:8191 |
| Ollama | http://127.0.0.1:11434 |

Successful setup checks real CUDA access, extension imports, Blender launch, model hashes, model digest/vision metadata, service identity, and registered graph input/model choices. It records results under `AI3D/State/install-*-verification.json` and `install-verification.json`. It does not certify an end-to-end generation until an actual user-selected asset has completed. See [what was tested](INSTALLATION_VALIDATION.md).

## Interrupted installation and existing services

Rerun the same command after network interruption. Hugging Face cache downloads and direct model downloads resume partial data. A server that ignores HTTP Range is handled by restarting that owned partial. A final model/archive is used only after size/SHA256 verification. Corrupt downloads are quarantined; existing final files with a different hash are preserved and reported. Tool extraction can resume after interruption.

Setup refuses to change environments while ComfyUI is already running on 8190/8191. Stop your known services first, then rerun; it never kills unknown GPU processes or removes production locks. The setup lock prevents two simultaneous installers. Existing source revisions are checked and never automatically reset. Existing databases and outputs are not erased.

An existing Ollama server can be reused only with the correct installed model identities. If required models are missing from that foreign server, quit it yourself before rerunning so the portable service can own its model store. Setup does not overwrite a mismatched model or download into an unknown running server's store.

Inspect the exact failure in `AI3D/Logs/Install/setup-*.log`, the associated service logs, and the verification JSON. Driver/CUDA failure requires correcting the NVIDIA driver and rerunning; it is not treated as successful CPU fallback. Authentication/access failures need your own upstream access. If available disk is insufficient, rerun in a directory on a larger drive. Do not delete model outputs to force success.

The bootstrap does not turn a partial setup into a successful one or weaken downstream quality checks. A full fresh-machine Windows/GPU install with all downloads has **not** been certified in this release; automated source checks and dependency resolution are separate evidence.

## Advanced / manual environments

`AI3D/Scripts/setup.ps1` remains available for core-only manual setup. The full entry point uses `-SkipModels` internally and performs the combined verified download afterward. Never put `AI3D/image-requirements.lock.txt` into the Hunyuan environment.

Individual launchers remain available. `Start-ImageProviders.ps1` accepts `LAS_IMAGE_COMFY_SOURCE` and `LAS_IMAGE_PYTHON`; unset defaults point to the local image installation. `LAS_BLENDER_PATH` supplies the portable Blender path to new job settings. Existing job settings remain intact. Inference runtimes remain on localhost.

Optional SAM experiments are not part of the required default U2Net workflow and are not downloaded by this installer. No Unreal Editor is required or installed. Nothing is imported into an Unreal project automatically.

## Your own catalog

The public catalog `AI3D/Prompts/catalog.example.json` is empty. The studio opens in custom-prompt mode. No game content or art direction is bundled; custom assets do not borrow a catalog style automatically.

Copy the template to a private JSON file and set `LAS_CATALOG_PATH` to its absolute path before launching if you want a catalog. Keep private catalogs outside the repository or under ignored `AI3D/Private/`.

The object contains `assets` (array), optional `shared_style` / `shared_negative` strings and optional `grid_cell_m` (positive number or null). Each asset needs a unique ASCII `asset_id` and nonempty `prompt`. Optional fields include `name_tr`, `name_en`, `category`, `category_profile`, `phase35_gate_profile`, `prompt_language`, style/negative overrides, `grid`, `footprint_m`, `height_m`, `period`, `polygon_target`, `polygon_range` and `texture_size`. Use JSON null for unknown dimensions/period. Grid conversion requires your own explicit cell size.

`period` is a free-form label. Texture sizes/safety limits are application settings rather than game design facts. Set exact per-asset dimensions/budgets when needed. Optional category profiles use explicit `category_profile` or your own entries in `AI3D/Config/category_profiles.json`; private benchmark results and asset maps are not bundled.

## LAN access

The studio generates its local access code in `AI3D/State/web-access-code.txt`. Other computers on the same trusted LAN use `http://<server-LAN-IP>:8088`. The optional `Enable-AssetStudioLAN.ps1` creates a private-network firewall rule when run with the necessary Windows rights. Setup does not silently change the firewall. ComfyUI/Ollama stay on localhost. Internet hosting/TLS/multi-user identities are outside this distribution's scope.
