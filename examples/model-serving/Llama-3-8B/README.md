Llama-3-8B input bundle.

Use:
- `--ref inputs/Llama-3-8B/reference`
- `--acc-checker inputs/Llama-3-8B/accuracy_checker`
- `--bench inputs/Llama-3-8B/benchmark`

Each folder contains scripts plus a short README.
- `README.md` — this file.

Expected files (for agent_system orchestrator):
- reference_inference.py
- accuracy_check.py
- benchmark.py
- config.json
- requirements.txt (GPU dependencies for verifier/benchmark)

The CLI reads these files from `inputs/Llama-3-8B` by default.

A separate `.venv/` is auto-created here by the verifier using `uv` with the dependencies from `requirements.txt` (torch, transformers, httpx).
