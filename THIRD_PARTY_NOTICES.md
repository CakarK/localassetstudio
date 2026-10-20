# Third-party notices

The project MIT license covers original Local Asset Studio code. It does not relicense model weights, external runtimes, dependencies or generated content.

## Bundled source

The browser viewer includes Three.js 0.170.0 and selected examples modules. Its MIT notice is preserved at `ai3d_manager/web_static/vendor/three/LICENSE`; package source, upstream/local file hashes and import adaptations are recorded in `SOURCE.json` in that directory.

## External components and models

These are installed or downloaded separately. Consult the exact upstream license/model card and the pinned revision; do not infer unrestricted commercial rights from this repository's license.

| Component | Upstream source / terms |
|---|---|
| Hunyuan3D 2.1 | [Tencent repository and Community License](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1) |
| Hunyuan ComfyUI integration | [visualbruno/ComfyUI-Hunyuan3d-2-1](https://github.com/visualbruno/ComfyUI-Hunyuan3d-2-1) |
| ComfyUI | [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) |
| DreamShaper 8 | [Lykon/dreamshaper-8](https://huggingface.co/Lykon/dreamshaper-8) |
| Qwen-Image-2512 | [Qwen model card](https://huggingface.co/Qwen/Qwen-Image-2512) and the file sources in `image_providers.json` |
| Z-Image-Turbo | Model repository, revision and license references in `AI3D/Config/image_providers.json` |
| DINOv2 | [facebook/dinov2](https://github.com/facebookresearch/dinov2) |
| Ollama / Qwen models | [Ollama](https://github.com/ollama/ollama), plus each installed model's own license and digest |
| Blender | [Blender license](https://www.blender.org/about/license/) |
| PyMeshLab | [cnr-isti-vclab/PyMeshLab](https://github.com/cnr-isti-vclab/PyMeshLab) |
| rembg / U-2-Net | [rembg](https://github.com/danielgatis/rembg), [U-2-Net](https://github.com/xuebinqin/U-2-Net) |

The optional MeshLib routes disabled by the compatibility script are not part of the supported production workflows. The Python dependency snapshot is in `AI3D/requirements.lock.txt`; each dependency retains its own terms.

External runtime source trees, binary wheels, model files and private generation data are not redistributed in this repository. Before redistributing any of those separately, review their own notice and license requirements. Keep upstream notices when applying the included compatibility patches.
