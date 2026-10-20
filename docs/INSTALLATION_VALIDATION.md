# Installer validation

The one-command installer is executable setup automation, not a claim that an untouched Windows machine has already completed every GPU workflow.

Validated for this source release:

- **307 tests passed; 9 optional installation/evidence checks skipped (316 discovered).** Existing source tests were retained. Seventeen installer-specific tests cover partial download resume, ignored Range responses, corrupt/unexpected file preservation, manifest identity, required model coverage, portable Blender settings, real Windows PowerShell parsing and interrupted archive extraction using small synthetic files.
- The core dependency set resolved for a fresh empty target: **149 packages**, with no installation performed.
- The separate image dependency lock resolved for a fresh empty target: **83 packages**, with no installation performed. Main image-runtime library versions match the working development image environment.
- Official upstream metadata supplied exact source revisions, sizes and SHA256 values for runtime archives and model files. Small model configuration files were fetched and hashed. U2Net's existing local bytes matched the upstream rembg MD5 and supplied the pinned SHA256. No new large model download was performed for this validation.
- The required CUDA tensor, native extension, Blender, model integrity and service/graph checks are implemented in `AI3D/Scripts/verify_install.py` and run on the destination computer. A setup failure is not reported as a successful installation.

Not certified by the above evidence:

- A full fresh-machine Windows install with all approximately 93 GB of model downloads and all runtime wheels.
- End-to-end image/shape/PBR generation with every pinned public dependency and the current public Qwen manifest. Existing development inference evidence is not reassigned to the newly pinned public Qwen revision.
- Other operating systems, CPU-only operation, less than 24 GB VRAM, automatic driver installation or public internet hosting.

GitHub Actions runs the installer contracts and PowerShell plan on a Windows runner without downloading GPU runtimes or model weights. The workflow result is separate from local validation and from actual inference evidence. Runtime reports are local-only under `AI3D/State/`; logs/model weights/user outputs are excluded from Git.
