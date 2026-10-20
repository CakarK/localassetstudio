# Installation

## Supported environment

**At least 24 GB of dedicated NVIDIA VRAM is required for the supported full pipeline.** Development uses Windows 11, RTX 3090 24 GB, approximately 48 GB system RAM and Blender 5.0.1. Other hardware/OS configurations and fresh-machine GPU installation are not certified. Plan disk space for multiple large model files plus isolated Python environments.

Install Git, [uv](https://docs.astral.sh/uv/), Blender and Ollama separately. Configure the NVIDIA driver for the CUDA runtime required by each environment. The installed CUDA Toolkit version and the PyTorch wheel's CUDA runtime are different things; do not replace a working environment merely to make their numbers match.

## 1. Core Hunyuan environment

Review `AI3D/sources.lock.json`, `AI3D/requirements.lock.txt` and the upstream licenses first. From the repository root:

```powershell
.\AI3D\Scripts\setup.ps1 -Python312 3.12.11
```

The setup script clones the pinned ComfyUI and Hunyuan integration revisions, creates `AI3D/.venv`, installs the locked dependencies and Windows extension wheels, applies the compatibility patches and downloads/validates Hunyuan/DINO weights. This is a large download, not a lightweight UI installation. Existing mismatched source revisions are not automatically reset. Some Hugging Face downloads may require your own authenticated access and acceptance of upstream terms.

The geometry environment is pinned to Python 3.12.11 / PyTorch 2.6.0 + CUDA 12.6 and ComfyUI v0.3.59. Do not upgrade it blindly. The supplied extension wheels are Windows/Python-version specific.

For DreamShaper, place `dreamshaper_8.safetensors` in `AI3D/Models/checkpoints/` or adjust the path in `image_providers.json` and the ComfyUI model-search configuration. Its expected size/SHA256 are recorded in the provider registry.

## 2. Separate modern image runtime

The pinned Hunyuan ComfyUI version is not the modern Z-Image/Qwen image runtime. Prepare a separate ComfyUI installation with the native nodes needed by your selected provider (`UNETLoader`, `CLIPLoader`, `VAELoader`, and the corresponding sampling/model support). The development image environment is an installed ComfyUI desktop backend with PyTorch 2.10.0 + CUDA 13.0; its separate source tree and environment are not distributed here. The Hunyuan lock file is **not** a lock file for this image environment.

Set the actual locations in PowerShell:

```powershell
$env:LAS_IMAGE_COMFY_SOURCE = 'D:\Tools\ImageComfyUI'  # contains main.py
$env:LAS_IMAGE_PYTHON = 'D:\Tools\ImageComfyUI\.venv\Scripts\python.exe'
```

If unset, the portable launchers expect `AI3D/ImageComfyUI/` and its `.venv/Scripts/python.exe`. Install the image runtime's dependencies yourself in that separate environment. The launcher starts only an existing environment; it does not download or upgrade packages.

Place the registry-specified diffusion model, encoder and VAE files under:

```text
AI3D/ImageRuntime/models/diffusion_models/
AI3D/ImageRuntime/models/text_encoders/
AI3D/ImageRuntime/models/vae/
```

`AI3D/Config/image_providers.json` contains exact filenames, sources, revisions, sizes and SHA256 values. You may point its file paths to an existing read-only model store; ComfyUI must also be configured to find those same files. Model weights are not included. The launcher generates a local model-path configuration if none exists.

## 3. Ollama

Start your local Ollama service on `127.0.0.1:11434`. Install `qwen3:4b` for CPU prompt/revision composition. Its requests use `num_gpu=0` and release residency after completion.

The VLM deployment uses the local tag `qwen3.6:27b` with its expected digest recorded in `AI3D/Config/quality.json`. This tag may be a local alias: do not assume it can be recreated with a public-library pull command. Install the appropriate model under its own terms, verify its identity and configure the actual tag/digest/protocol before using visual evaluation. The code deliberately refuses identity/resource mismatches. An alternative model is not automatically equivalent to the tested configuration.

## 4. Blender and catalog

The default Windows Blender path is `C:/Program Files/Blender Foundation/Blender 5.0/blender.exe`. Adjust the existing settings when your installation differs. No Unreal Editor is needed for ordinary exports.

### Your own catalog

The default `AI3D/Prompts/catalog.example.json` is an empty template. The studio starts in custom-prompt mode when the catalog is empty. No game content or art direction is bundled, and custom assets never borrow a catalog's style automatically.

To use a catalog, copy the template to a private JSON file, add your own assets, then set `$env:LAS_CATALOG_PATH` to its absolute path before starting the studio. Keep private catalogs outside this repository or under ignored `AI3D/Private/`.

The top-level object has `assets` (an array), optional `shared_style` / `shared_negative` strings, and optional `grid_cell_m` (positive number or null). Each asset needs a unique ASCII `asset_id` and a nonempty `prompt`. Optional asset fields include `name_tr`, `name_en`, `category`, `category_profile`, `phase35_gate_profile`, `prompt_language`, `shared_style`, `shared_negative`, `grid`, `footprint_m`, `height_m`, `period`, `polygon_target`, `polygon_range` and `texture_size`. Use JSON null for unknown dimensions or period. Grid conversion requires your explicit `grid_cell_m`; no game-specific cell size is assumed. Asset overrides are recorded as explicit overrides.

`period` is an optional free-form label. Technical defaults such as texture size and safety limits are application settings, not game design facts. Set exact per-asset polygon/dimension values when required.

Optional category experiments require an explicit `category_profile` (`Buildings`, `Towers`, `Organic`, `Debris`, `Industrial`, `Props`, `Vehicles`, `Landmarks`) or a user-defined entry in `AI3D/Config/category_profiles.json`. These maps ship empty and no private benchmark result selects your provider automatically.

The JSON catalog replaces the private developer-specific Markdown catalog parser. Existing private Markdown packs must be converted to this schema locally; none are included in this distribution.

## 5. Start and verify

```powershell
.\Start-AI3D.ps1 -NoBrowser
.\Start-ImageProviders.ps1 -NoBrowser
.\Start-AssetStudio.ps1 -NoBrowser
```

Open `http://localhost:8088`. The first studio start generates its own access code and secret in `AI3D/State/`; never commit them. Check the ComfyUI `system_stats` and `object_info` endpoints on ports 8190/8191 before submitting a real job. Test a single reference/asset before building a batch.

Stop the studio with `Stop-AssetStudio.ps1`; `Stop-AI3D.ps1` targets the core server tracked by this installation. Stopping the web server does not necessarily terminate separately running production workers. Do not kill unknown GPU processes or delete locks to force a retry.

## LAN access

The studio binds to the network interface and checks the Host, session, CSRF and Origin. On a trusted Windows private network, `Enable-AssetStudioLAN.ps1` can permit TCP 8088. Other computers use `http://<server-LAN-IP>:8088` with the local access code. ComfyUI and Ollama remain bound to localhost. Do not expose this development workbench directly to the internet.
