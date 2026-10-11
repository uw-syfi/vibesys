# show-o2-1.5B-HQ

Text-to-image serving of `showlab/show-o2-1.5B-HQ` behind an OpenAI-like
`/v1/images/generations` endpoint; the metric is p50 request latency.

One project, three tasks under `.vibesys/tasks/`. They share the evaluator layout and the
project-root `config.json` and `requirements.txt`, and differ in the objective and the
hardware target:

| Task | Target |
| --- | --- |
| `default` | Target-agnostic objective. Its `reference/` holds the pinned `Show-o` git submodule. |
| `h100` | A single NVIDIA H100 (CUDA graphs, diffusion-token trimming, VAE layout). |
| `macbook` | Apple Silicon (MLX / Metal, cross-step caches, quantization). |

Copy the project with `vibesys example new model-serving/show-o2-1.5B-HQ DEST`, then run one
task from `DEST` (each task has its own README):

```bash
vibesys --runs-dir /work/vibesys-runs --local --task default
vibesys --runs-dir /work/vibesys-runs --local --task h100
vibesys --runs-dir /work/vibesys-runs --local --task macbook
```
