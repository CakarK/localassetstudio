# Contributing to Local Asset Studio

Read [the installation guide](docs/INSTALLATION.md) before running inference. Full production requires a supported **24 GB VRAM NVIDIA GPU**, separate Hunyuan/image environments, Blender and local Ollama models.

Keep changes focused and preserve these contracts:

- Human acceptance is separate from technical validation; never manufacture PASS results or completed stages.
- File hashes, masks, queue/GPU ownership, CSRF and same-origin protection remain enforced.
- Keep previous outputs. A review decision produces at most one automatic regeneration; restarting the server must not multiply jobs.
- GPU jobs run serially. Do not unload another request's models or interrupt foreign ComfyUI work.
- New object assets should contain no terrain/base/pedestal geometry by default.
- Do not import anything into a user's Unreal project automatically.
- Do not commit access codes, `.env` files, databases, logs, outputs, private catalogs, model weights or external runtime environments.

Run isolated tests from the repository root using a configured Python environment:

```powershell
python -m unittest discover -s tests -v
```

Tests use synthetic fixtures/mocked services. Label actual inference evidence separately. Do not run production-lock integration checks while real jobs are active. Add a regression test for meaningful behavior changes, and describe what was validated versus what still requires real hardware.

For issues, include the stage, provider, relevant software versions and sanitized error category. Share only the smallest logs needed; remove access/session credentials and personal paths. Do not upload model weights.

Nine checks require the original installation's preservation snapshots, hardware evidence or external ComfyUI source. A source-only clone explicitly skips them when those artifacts are absent; their assertions still run when the artifacts are installed. Skipped installation checks are not reported as PASS.
