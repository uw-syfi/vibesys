# vibe-serve-skills

Agent skills for LLM / multimodal **serving-system development**. For use with Claude Code, Cursor, Codex, Gemini CLI, and other tools that understand the Agent Skills format.

This collection covers the layers an engineer works through when building or extending a serving engine — model architectures, serving algorithms, implementation tooling, and reference engines — without duplicating kernel-level material already covered by [agent-gpu-skills](https://github.com/slowlyC/agent-gpu-skills) (CUDA / Triton / CUTLASS).

**New to serving performance?** Start with [OVERVIEW.md](OVERVIEW.md) — it covers the roofline model, why decode is memory-bound while prefill is compute-bound, and how to navigate the skills by the bottleneck you're trying to fix.

## Organization

Skills are organized by **abstraction layer** with explicit extensibility axes.

| Tier | Axis | Purpose |
|:-----|:-----|:--------|
| [`models/`](references/models/) | model architecture | What does each model look like? Weight layout, attention type, tokenization, modalities. |
| [`algorithms/`](references/algorithms/) | idea / algorithm | Cross-cutting serving concepts: continuous batching, paged attention, speculative decoding, MoE routing, parallelism, quantization schemes. |
| [`frameworks/`](references/frameworks/) | programming framework | PyTorch / MLX / (future JAX) idioms for serving. |
| [`backends/`](references/backends/) | software backend library | How to **use** existing kernel libraries — FlashInfer, FlashAttention, Triton kernels, CUDA graph. Kernel *implementation* is out of scope; see agent-gpu-skills. |
| [`hardware/`](references/hardware/) | hardware platform | Hopper / Blackwell / MI300 / Apple Silicon specifics — precision, collectives, tuning. |
| [`engines/`](references/engines/) | reference system | Source-code lookup into vLLM, SGLang, TensorRT-LLM. Short SKILL.md + "where's X" grep tables. |
| [`tooling/`](references/tooling/) | orthogonal workflow | FastAPI serving, accuracy checking, serving benchmarks, profiling, I/O handling. |

## Extensibility

Each tier is designed so new entries drop in as folders:

- **Add a model** → new folder under `models/` describing arch + features it needs.
- **Add hardware** → new folder under `hardware/` with precision / collective / profiler notes.
- **Add a framework or backend** → new folder under `frameworks/` or `backends/`.
- **Add an engine** → new folder under `engines/` with "where's X" tables.

Because axes are not fully orthogonal (FlashInfer is CUDA-only, MLX is Apple-only, MLA needs a MLA-capable backend), each `algorithms/` skill ends with a compatibility matrix (`algorithm × {backend, hardware, engine}`) so cross-axis constraints are captured where they belong.

## Kernel-level boundary

This collection assumes existing kernel libraries. Writing new CUDA / Triton / CUTLASS kernels is **out of scope** — those skills live in [agent-gpu-skills](https://github.com/slowlyC/agent-gpu-skills). Each `backends/*` skill ends with a pointer back to the relevant gpu-skills entry.

## Setup

The vibeserve agent CLIs auto-load this skill from `skills/serving-systems/`
via the `--skills-dir` flag (default in `vibe_serve/cli_common.py`),
copying the skill tree into each workspace's `.claude/skills/` so the
in-workspace coding agent picks it up.

The reference engines (`repos/{vllm,sglang,TensorRT-LLM}/`) are tracked as
git submodules — initialize with:

```bash
git submodule update --init skills/serving-systems/repos
```

`update-repos.sh` is the upstream sparse-checkout helper, kept for parity
with the source `vibe-serve-skills` repo; the submodule flow above is the
one used here.

## Directory structure

```
vibe-serve-skills/
├── README.md, CLAUDE.md          # overview + guidance for skill authors
├── update-repos.sh               # upstream sparse-checkout helper (parity)
│
├── models/                       text-dense, text-moe, ssm-hybrid,
│                                 vision-language, speech-language,
│                                 image-generation, video-generation,
│                                 speech-generation, omni-multimodal
├── algorithms/                   attention-variants, async-scheduling,
│                                 continuous-batching, paged-attention,
│                                 radix-prefix-caching, heterogeneous-kv-cache,
│                                 chunked-prefill, speculative-decoding,
│                                 disaggregated-serving, moe-routing-dispatch,
│                                 quantization-schemes, parallelism,
│                                 structured-output, batched-sampling
├── frameworks/                   pytorch, triton, mlx
├── backends/                     flashinfer, flashattention, sdpa,
│                                 triton-kernels, cuda-graph
├── hardware/                     nvidia, amd-mi300, apple-silicon
├── engines/                      vllm, sglang, trtllm
├── tooling/                      fastapi-serving, openai-api,
│                                 accuracy-checker, serving-benchmark,
│                                 profiler, io-handling, lora-serving
└── repos/                        vllm, sglang, TensorRT-LLM (git submodules)
```

## Authoring

See [CLAUDE.md](CLAUDE.md) for skill-authoring conventions used in this repo.
