# TRELLIS-only migration (v0.3.0)

Hunyuan and DINOv2 are removed from the model/source lock, setup, selectable providers and executable ComfyUI mesh graphs. New jobs use TRELLIS.2; old Hunyuan jobs fail with ProviderRemoved rather than changing provenance. Historical databases remain readable. Model acceptance remains a human choice; technical/AI failures are retained.

## Install

Use `Install-LocalAssetStudio.ps1 -Plan` first, then `Install-LocalAssetStudio.ps1`. Three isolated environments serve the application/classic images, modern reference images and TRELLIS. No inference is submitted by setup.

Requirements: Windows 11 x64; RTX 3090 24 GB (the bundled native kernels target sm_86); enough NVIDIA driver support for CUDA 13.0; 120 GiB free disk. The pinned model download total is in `AI3D/install.lock.json`; Python packages and tools are additional. Other GPU architectures require native builds from the included corresponding sources, not merely changing a setting.

DINOv3: obtain free approved access at https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m. Accept its terms using your own account. Setup can prompt for local Hugging Face login; never publish tokens. Gated denial stops setup and can be resumed after approval. DINOv3 terms are separate from TRELLIS's MIT code license.

Native wheels are pinned by size/SHA256 in `AI3D/TrellisRuntime/wheels/manifest.json`. Corresponding source is included in `native-sources.zip`; its digest and upstream revision/patch records are alongside it. O-Voxel includes the CPU UV rasterizer and original dense-surface projection fix. No BRIA, MeshLib or NVIDIA rasterizer is installed by this integration.

## Existing installations

Stop studio/Comfy services before changing runtimes. Back up any assets you want to keep. This release does **not** silently delete user assets or force-migrate saved jobs. Remove Hunyuan model weights, DINOv2-only caches and its custom node directory after confirming no other application uses them. Never delete a shared Hugging Face or Comfy store wholesale. New TRELLIS jobs may reuse selected reference images; they do not reuse Hunyuan meshes.

The developer's private cleanup was explicitly authorized and separately logged; it is not run on other users' machines by the installer. Old Hunyuan models were deleted there at the user's request. TRELLIS outputs and model weights were preserved.

## Validation limits

Real TRELLIS Shape/PBR/export were exercised on two local structural assets before release packaging. One candidate has significant roof/topology issues: technical export success does not imply game-ready art. QA/VLM reports and human review remain necessary. The public bootstrap/model download/native wheel import changes have contract checks; a completely fresh full GPU installation has not been certified. No universal commercial-use guarantee is made: every dependency, input and model keeps its own license.


For a native rebuild, verify `native-sources.sha256`, extract `native-sources.zip` into `AI3D/TrellisRuntime/src`, install MSVC C++ tools and a matching CUDA 13 toolkit, then run `AI3D/TrellisRuntime/build_native.ps1 -CudaArchitectures <your-architecture>` using uv. This is an expert path; the bundled 8.6 wheels remain the validated default. Never install these wheels into a different PyTorch/CUDA environment.
