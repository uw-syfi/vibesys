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
| [`frameworks/`](references/frameworks/) | programming framework | PyTorch / MLX / (future JAX) idioms for serving. |
| [`backends/`](references/backends/) | software backend library | How to **use** existing kernel libraries — FlashInfer, FlashAttention, Triton kernels, CUDA graph. Kernel *implementation* is out of scope; see agent-gpu-skills. |
| [`hardware/`](references/hardware/) | hardware platform | Hopper / Blackwell / MI300 / Apple Silicon specifics — precision, collectives, tuning. |
| [`engines/`](references/engines/) | reference system | Source-code lookup into vLLM, SGLang, TensorRT-LLM. Short notes + "where's X" grep tables. |
| [`tooling/`](references/tooling/) | orthogonal workflow | FastAPI serving, accuracy checking, serving benchmarks, profiling, I/O handling. |

## Extensibility

Each tier is designed so new entries drop in as reference files:

- **Add a model** → new note under `references/models/` describing arch + features it needs.
- **Add hardware** → new note under `references/hardware/` with precision / collective / profiler notes.
- **Add a framework or backend** → new note under `references/frameworks/` or `references/backends/`.
- **Add an engine** → new note under `references/engines/` with "where's X" tables.

Because axes are not fully orthogonal (FlashInfer is CUDA-only, MLX is Apple-only, MLA needs a MLA-capable backend), each `references/algorithms/` note ends with a compatibility matrix (`algorithm × {backend, hardware, engine}`) so cross-axis constraints are captured where they belong.

## Kernel-level boundary

This collection assumes existing kernel libraries. Writing new CUDA / Triton / CUTLASS kernels is **out of scope** — those skills live in [agent-gpu-skills](https://github.com/slowlyC/agent-gpu-skills). Each `references/backends/*` note ends with a pointer back to the relevant gpu-skills entry.

## Setup

Inside an agent workspace, this collection appears as the `serving-systems/`
skill under the per-CLI skill-discovery paths (`.claude/skills/`,
`.agents/skills/`, …); every path in this document is relative to that skill
root. The vibesys agent CLIs copy it there automatically — in the VibeSys
repo checkout, the collection lives at `resources/skills/serving-systems/`
and is picked up via the `--skills-dir` flag (default candidate root
`resources/skills/`, defined in `src/vibesys/skills.py`).

The reference engines (`repos/{vllm,sglang,TensorRT-LLM}/`) are tracked as
git submodules and are not copied into workspaces — from the repo checkout,
initialize with:

```bash
git submodule update --init resources/skills/serving-systems/repos
```

`update-repos.sh` is the upstream sparse-checkout helper, kept for parity
with the source `vibesys-skills` repo; the submodule flow above is the
one used here.

## Directory structure

```
serving-systems/
├── SKILL.md                      # the single skill; routes to references/
├── README.md, OVERVIEW.md,       # repo docs + guidance for skill authors
│   CLAUDE.md
├── update-repos.sh               # upstream sparse-checkout helper (parity)
├── references/
│   ├── models/                   text-dense, text-moe, ssm-hybrid,
│   │                             vision-language, speech-language,
│   │                             image-generation, video-generation,
│   │                             speech-generation, omni-multimodal
│   ├── algorithms/               attention-variants, async-scheduling,
│   │                             continuous-batching, paged-attention,
│   │                             radix-prefix-caching, heterogeneous-kv-cache,
│   │                             chunked-prefill, speculative-decoding,
│   │                             disaggregated-serving, moe-routing-dispatch,
│   │                             quantization-schemes, parallelism,
│   │                             structured-output, batched-sampling
│   ├── frameworks/               pytorch, triton, mlx, neuron-pytorch,
│   │                             neuron-flash-attention, nxd-inference,
│   │                             nxd-kv-cache
│   ├── backends/                 flashinfer, flashattention, sdpa,
│   │                             triton-kernels, cuda-graph,
│   │                             attention-backend-comparison
│   ├── hardware/                 nvidia, amd-mi300, apple-silicon,
│   │                             aws-trainium
│   ├── engines/                  vllm, sglang, trtllm
│   └── tooling/                  fastapi-serving, openai-api,
│                                 accuracy-checker, serving-benchmark,
│                                 profiler, io-handling, lora-serving
└── repos/                        vllm, sglang, TensorRT-LLM (git submodules)
```

## Authoring

See [CLAUDE.md](CLAUDE.md) for skill-authoring conventions used in this repo.
