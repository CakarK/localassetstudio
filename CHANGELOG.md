# Changelog

## 0.3.0 — 2026-10-22

- TRELLIS.2 is the sole 3D backend. Hunyuan/DINOv2 model downloads, custom nodes, mesh/PBR graphs and seven exclusive dependency pins were removed. Old jobs retain provenance and cannot execute a removed provider.
- Added the validated isolated 512 Shape/latent/PBR runtime, CuMesh optimization, dense-source texture projection, provider metadata, and Blender 5.2.2 selection. Existing human reference and model approval rules remain.
- One-click `Install-LocalAssetStudio.bat`: runs beside source, or bootstraps the versioned release as a standalone file. `Start-LocalAssetStudio.bat` starts the installation or invokes setup if missing.
- Free DINOv3 account access is checked before large image/3D downloads. Missing login opens the terms page and prompts locally; pending approval stops safely and can be resumed. No approval bypass.
- Bootstrap never runs an older Hunyuan installer: an older installation and its user data remain in place, while v0.3.0 installs to a versioned sibling.
- Native wheel SHA256/size locks and corresponding source/licenses are shipped for Windows CPython 3.12 / torch 2.10+cu130 / sm_86 (RTX 3090). Other architectures need a matching native build. 24 GB VRAM remains required.
- No private game catalog, prompts, model weights, generated assets, tokens or production databases are published. Public setup does not automatically delete user assets.

Validation: native wheels installed into a separate target and imported against the real PyTorch runtime, including the dense-source PBR fix. Double-click BAT paths (with spaces), failure propagation, first start, and old-install preservation exercised using synthetic installers. Final full suite: 344 discovered, 338 passed, 6 optional historical/installation evidence checks skipped, 112.003 seconds. Separate double-click/installer subset: 28/28 passed. A completely fresh full GPU setup remains uncertified; generative quality is not guaranteed by technical export checks.
