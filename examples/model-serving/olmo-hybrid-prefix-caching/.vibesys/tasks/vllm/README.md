Olmo-Hybrid-7B prefix-caching input bundle.

Use:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --input examples/model-serving/olmo-hybrid-prefix-caching --task vllm
```

Workload: 20 concurrent requests, 32 768-token shared prefix + 128-token unique tail per request, 128 generated tokens. See `OBJECTIVE.md`.

Expected task files:
- reference/reference.py (verbatim transformers `modeling_olmo_hybrid.py`)
- reference/config.json
- reference/meta.json (HF model id: `allenai/Olmo-Hybrid-7B`)
- accuracy_checker/checker.py
- benchmark/benchmark.py

The project root holds `requirements.txt` and `pyproject.toml` (the uv environment of the checker and benchmark).
