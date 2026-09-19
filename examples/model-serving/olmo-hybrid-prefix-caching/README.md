Olmo-Hybrid-7B prefix-caching input bundle.

Use:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --input examples/model-serving/olmo-hybrid-prefix-caching
```

Workload: 20 concurrent requests, 32 768-token shared prefix + 128-token unique tail per request, 128 generated tokens. See `.vibesys/tasks/default/OBJECTIVE.md`.

Single task `default` (selected automatically). Layout:
- .vibesys/tasks/default/reference/reference.py (verbatim transformers `modeling_olmo_hybrid.py`)
- .vibesys/tasks/default/reference/config.json
- .vibesys/tasks/default/reference/meta.json (HF model id: `allenai/Olmo-Hybrid-7B`)
- .vibesys/tasks/default/accuracy_checker/checker.py
- .vibesys/tasks/default/benchmark/benchmark.py
- requirements.txt and pyproject.toml (project root, candidate-writable)
