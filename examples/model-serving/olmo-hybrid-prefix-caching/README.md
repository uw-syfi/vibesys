# olmo-hybrid-prefix-caching

Prefix-caching serving of `allenai/Olmo-Hybrid-7B` (attention plus linear-attention layers):
20 concurrent requests, each a 32 768-token shared prefix plus a 128-token unique tail, 128
generated tokens. The metric is `aggregate_throughput_tok_per_sec`.

One project, two tasks under `.vibesys/tasks/`. They share the evaluator and the Python
environment (`pyproject.toml`, `requirements.txt`) and differ only in the objective and the
starting point:

| Task | Starting point |
| --- | --- |
| `from-scratch` | The agent writes the serving system itself. |
| `vllm` | The workspace holds a pinned vLLM checkout (`vllm/`) to start from. |

Run one task from this directory (each task has its own README):

```bash
vibesys --runs-dir /work/vibesys-runs --local --task from-scratch
vibesys --runs-dir /work/vibesys-runs --local --task vllm
```
