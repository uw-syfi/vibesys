Olmo-Hybrid-7B prefix-caching input bundle.

Use:

```bash
vibe-sys --input examples/model-serving/olmo-hybrid-prefix-caching
```

Workload: 20 concurrent requests, 32 768-token shared prefix + 128-token unique tail per request, 128 generated tokens. See `OBJECTIVE.md`.

Expected files:
- reference/reference.py (verbatim transformers `modeling_olmo_hybrid.py`)
- reference/config.json
- reference/meta.json (HF model id: `allenai/Olmo-Hybrid-7B`)
- accuracy_checker/checker.py
- benchmark/benchmark.py
- requirements.txt
