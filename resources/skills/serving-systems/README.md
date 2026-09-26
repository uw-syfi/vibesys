# vibesys-skills

Agent skills for LLM / multimodal **serving-system development**. For use with Claude Code, Cursor, Codex, Gemini CLI, and other tools that understand the Agent Skills format.

This collection covers the layers an engineer works through when building or extending a serving engine — model architectures, serving algorithms, implementation tooling, and reference engines — without duplicating kernel-level material already covered by [agent-gpu-skills](https://github.com/slowlyC/agent-gpu-skills) (CUDA / Triton / CUTLASS).

**New to serving performance?** Start with [OVERVIEW.md](OVERVIEW.md) — it covers the roofline model, why decode is memory-bound while prefill is compute-bound, and how to navigate the skills by the bottleneck you're trying to fix.

## Organization

Skills are organized by **abstraction layer** with explicit extensibility axes.

| Tier | Axis | Purpose |
|:-----|:-----|:--------|
| [`models/`](references/models/) | model architecture | What does each model look like? Weight layout, attention type, tokenization, modalities. |
| [`algorithms/`](references/algorithms/) | idea / algorithm | Cross-cutting serving concepts: continuous batching, paged attention, speculative decoding, MoE routing, parallelism, quantization schemes. |
| [`frameworks/`](references/frameworks/) | programming framework | **Cross-platform only** — PyTorch and Triton idioms for serving. Platform-bound frameworks (MLX, torch-neuronx, NxD) live under that platform's directory. |
| [`platforms/`](references/platforms/) | compute backend | One directory per `ComputeBackend` (`cuda`, `rocm`, `trainium`, `metal`, `cpu`), each with `floor.md`, `hardware.md`, `profiler.md` plus its own kernel-library notes. Only the selected backend's directory is materialized. Kernel *implementation* is out of scope; see agent-gpu-skills. |
| [`engines/`](references/engines/) | reference system | Source-code lookup into vLLM, SGLang, TensorRT-LLM. Short SKILL.md + "where's X" grep tables. |
| [`tooling/`](references/tooling/) | orthogonal workflow | FastAPI serving, accuracy checking, serving benchmarks, profiling, I/O handling. |

## Extensibility

Each tier is designed so new entries drop in as folders:

- **Add a model** → new file under `models/` describing arch + features it needs.
- **Add a platform** → new `platforms/<backend>/` directory, named for the exact `ComputeBackend` value (`cuda`, not `nvidia`). It must carry the full skeleton — `floor.md`, `hardware.md`, `profiler.md` — or `validate_skill_tree` fails the run. See [`CLAUDE.md`](CLAUDE.md).
- **Add a cross-platform framework** → new file under `frameworks/`. Platform-bound ones go in `platforms/<backend>/`.
- **Add an engine** → new file under `engines/` with "where's X" tables.

Because axes are not fully orthogonal (FlashInfer is CUDA-only, MLX is Apple-only, MLA needs an MLA-capable backend), each `algorithms/` file ends with a compatibility matrix (`algorithm × {kernel library, engine, backend}`) so cross-axis constraints are captured where they belong. **N/A rows are load-bearing** — "this does not apply on `trainium`, use X instead" is what stops a backend silently inheriting another's guidance.

## Kernel-level boundary

This collection assumes existing kernel libraries. Writing new CUDA / HIP / Triton / CUTLASS kernels is **out of scope** — those skills live in [agent-gpu-skills](https://github.com/slowlyC/agent-gpu-skills). Each kernel-library file under `platforms/<backend>/` ends with a pointer back to the relevant gpu-skills entry.

**Exception — NKI:** writing NeuronCore kernels for AWS Trainium *is* in scope, via the bundled `neuron-nki-*` skills.

## Setup

The vibesys agent CLIs auto-load this skill from `skills/serving-systems/`
via the `--skills-dir` flag (default in `vibesys/cli_common.py`),
copying the skill tree into each workspace's `.claude/skills/` so the
in-workspace coding agent picks it up.

The reference engines (`repos/{vllm,sglang,TensorRT-LLM}/`) are tracked as
git submodules — initialize with:

```bash
git submodule update --init --checkout resources/skills/serving-systems/repos
```

`update-repos.sh` is the upstream sparse-checkout helper, kept for parity
with the source `vibesys-skills` repo; the submodule flow above is the
one used here.

## Directory structure

```
vibesys-skills/
├── README.md, CLAUDE.md          # overview + guidance for skill authors
├── update-repos.sh               # upstream sparse-checkout helper (parity)
│
├── models/                       attention-variants, text-dense, text-moe,
│                                 ssm-hybrid,
│                                 vision-language, speech-language,
│                                 image-generation, video-generation,
│                                 speech-generation, omni-multimodal
├── algorithms/                   async-scheduling, continuous-batching,
│                                 paged-attention, radix-prefix-caching,
│                                 heterogeneous-kv-cache, chunked-prefill,
│                                 speculative-decoding, disaggregated-serving,
│                                 moe-routing-dispatch, quantization-schemes,
│                                 parallelism, structured-output,
│                                 batched-sampling, cross-attention-kv-cache
├── frameworks/                   pytorch, triton          (cross-platform only)
├── platforms/                    ONE dir per ComputeBackend; only the
│   ├── cuda/                     selected backend is materialized.
│   │                             floor, hardware, profiler + flashinfer,
│   │                             flashattention, sdpa, cuda-graph,
│   │                             triton-kernels, attention-backend-comparison
│   ├── rocm/                     floor, hardware, profiler, aiter,
│   │                             measurement-protocol, counter-triage,
│   │                             roofline, aiter-engagement
│   ├── trainium/                 floor, hardware, profiler + neuron-pytorch,
│   │                             nxd-inference, nxd-kv-cache,
│   │                             neuron-flash-attention
│   ├── metal/                    floor, hardware, profiler, mlx, mlx-serving
│   └── cpu/                      floor, hardware, profiler
├── engines/                      vllm, sglang, trtllm
├── tooling/                      fastapi-serving, openai-api,
│                                 accuracy-checker, serving-benchmark,
│                                 profiler, io-handling, lora-serving
└── repos/                        vllm, sglang, TensorRT-LLM (git submodules)
```

## Authoring

See [CLAUDE.md](CLAUDE.md) for skill-authoring conventions used in this repo.
