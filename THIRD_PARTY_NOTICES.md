# Third-party notices

Local Asset Studio code is MIT. Other components and model weights retain their own terms; this is not a blanket commercial-use warranty.

| Component | License/source reference |
|---|---|
| TRELLIS.2 / weights | MIT; https://github.com/microsoft/TRELLIS.2 and https://huggingface.co/microsoft/TRELLIS.2-4B |
| TRELLIS image-large decoder | MIT; https://huggingface.co/microsoft/TRELLIS-image-large |
| CuMesh / cubvh / FlexGEMM | Upstream MIT notices and pinned revisions included with native sources |
| Eigen headers in native builds | MPL-2.0; corresponding source included in `AI3D/TrellisRuntime/native-sources.zip` |
| DINOv3 | Separate Meta DINOv3 terms and gated account approval: https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m |
| BiRefNet | MIT; https://huggingface.co/ZhengPeng7/BiRefNet |
| PyTorch / torchvision / CUDA / Triton | Their own BSD-style/runtime terms; pinned package metadata in `AI3D/TrellisRuntime/licenses/` |
| ComfyUI | GPL upstream: https://github.com/Comfy-Org/ComfyUI |
| DreamShaper / Qwen-Image / Z-Image | Model/file/license references in `AI3D/Config/image_providers.json` |
| Blender | GPL; https://www.blender.org/about/license/ |
| Ollama / Qwen | https://github.com/ollama/ollama and each model's own terms |
| PyMeshLab / rembg / U2Net | Each upstream package retains its own license |

Hunyuan, DINOv2 and their custom rasterizer/inpaint packages are no longer installed or selectable. Historical source history is not a license for old generated assets; users must assess prior outputs separately.

This release includes selected MIT TRELLIS Python source and validated native Windows wheels. Their corresponding source, modifications and license notices are included beside the wheels. The O-Voxel wheel has explicit local changes for CPU UV rasterization and dense-mesh projection; its RECORD and wheel hash were regenerated. Third-party model weights, private game data, credentials and generated assets are not redistributed.
