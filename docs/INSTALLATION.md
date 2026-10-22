# Installation

Local Asset Studio v0.3.0 uses TRELLIS.2, not Hunyuan. **24 GB NVIDIA VRAM is required.** The bundled native wheels target Windows 11 x64 / RTX 3090 (sm_86) / CPython 3.12 / torch 2.10 CUDA 13.0. Other architectures need native builds.

Download the release `Install-LocalAssetStudio.bat` and double-click it, or use the same BAT in the source ZIP. Existing older installations use a preserved sibling directory; they are never dispatched as the new installer.

```powershell
# From a cloned or downloaded source tree:
.\Install-LocalAssetStudio.ps1 -Plan
.\Install-LocalAssetStudio.ps1
```

The existing `install.ps1` bootstrap downloads a specific main-branch commit to a new installation directory and invokes the same installer. Downloads and model files are checked against pinned sizes/hashes. Setup resumes existing valid files and refuses to overwrite mismatched files.

DINOv3 access approval is mandatory and free; it cannot be automated around account terms. Approve https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m and log in locally when requested. The installer is one entry point, not an approval-free unattended installation.

Allow 120 GiB free disk. Exact model bytes are in `AI3D/install.lock.json`; Python packages and tools are additional. Setup creates core, image and TRELLIS environments; installs Blender 5.2.2 and Ollama; obtains reference/VLM/3D weights; verifies native wheels; then starts localhost services and the LAN studio. No production inference is submitted.

[Migration, licenses, native sources and validation limits](TRELLIS_MIGRATION.md). Existing outputs/databases are not erased by public setup. Clean-machine full GPU installation remains uncertified.
