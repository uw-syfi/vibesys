Llama-3-8B input bundle.

Use:

```bash
vibesys --input examples/model-serving/Llama-3-8B
```

Each folder contains scripts plus a short README.
- `README.md` — this file.

Expected layout (declared by `vibesys.input.toml`):
- OBJECTIVE.md — deployment goal for the run
- vibesys.input.toml — manifest with the accuracy and benchmark commands
- reference/ — reference implementation (`reference.py`, `config.json`, `meta.json`)
- accuracy_checker/ — `checker.py`, the correctness gate
- benchmark/ — `benchmark.py`, emits the metric to optimize
- config.json
- requirements.txt (GPU dependencies for verifier/benchmark)

The CLI reads the manifest from this input bundle and runs the declared
accuracy and benchmark commands.

A separate `.venv/` is auto-created here by the verifier using `uv` with the dependencies from `requirements.txt` (torch, transformers, httpx).
