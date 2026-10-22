# Validated native binaries

Built locally from the pinned audited CuMesh, FlexGEMM and O-Voxel sources. Windows x64 / CPython 3.12 / PyTorch 2.10.0+cu130 / sm_86. RTX 3090 was exercised. Other architectures are not certified. Check manifest.json before install. Corresponding source and licenses are in ../native-sources.zip and ../licenses/. O-Voxel includes the CPU rasterizer and dense-source PBR projection modification; no NVIDIA rasterizer is needed.
