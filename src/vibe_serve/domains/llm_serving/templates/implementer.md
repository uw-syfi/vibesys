Model weights are at `/model` — do NOT download models.

The Judge also runs a standard accuracy check and benchmark sanity test in addition to this round's pass criteria. Your implementation must pass those too.

## Required: read the relevant skill BEFORE writing code

The `serving-systems` skill is installed in your working directory with a `references/` library covering every kernel, library, algorithm, and technique relevant to this work. **You must consult the relevant references before you write any code that touches them. This is not optional.**

The references library lives at `references/<tier>/<topic>.md` (the `serving-systems` skill's `SKILL.md` body is the index). Tiers: `algorithms`, `backends`, `frameworks`, `hardware`, `models`, `engines`, `tooling`.

**Before writing or modifying code, open every reference that covers a topic named in the task.** Some examples — these are not exhaustive:

- Task says "CUDA graphs" / "graph capture" / "graph replay" → open `references/backends/cuda-graph.md`.
- Task says "FlashAttention" / "FlashInfer" / "swap attention backend" / "fused attention" → open `references/backends/attention-backend-comparison.md` first (the picker), then the per-backend reference (`flashattention.md`, `flashinfer.md`, or `sdpa.md`) for whichever you commit to.
- Task says "EAGLE3" / "spec decoding" / "draft model" / "MTP" → open `references/algorithms/speculative-decoding.md` *thoroughly*. Read the section on draft-vocab-to-target mapping (`d2t`/`t2d`) and the auxiliary-hidden-state handoff before you write a single line — those two failure modes alone are responsible for most "EAGLE3 wired but 0 acceptance" outcomes in this loop.
- Task says "xgrammar" / "structured output" / "JSON schema" / "grammar mask" → open `references/algorithms/structured-output.md`.
- Task says "paged attention" / "block table" / "KV cache pages" → open `references/algorithms/paged-attention.md`.
- Task says "continuous batching" / "scheduler" → open `references/algorithms/continuous-batching.md`.
- Task says "torch.compile" / "PyTorch idioms" → open `references/frameworks/pytorch.md`.
- Task says "nsys" / "Nsight" / "torch profiler" / "where is the time going" → open `references/tooling/profiler.md`.

**Coding from priors is the single most common reason this loop wastes rounds.** Concrete failure modes already observed:

- Implementer wrote SDPA-only attention for 24 rounds because no one opened `references/backends/flashattention.md` — leaving 3-5× perf on the table.
- Implementer wired EAGLE3 with 0 acceptance and abandoned it, because no one read the `d2t`/`t2d` section of `references/algorithms/speculative-decoding.md` — leaving another 2× on the table.
- Implementer guessed CUDA-graph capture semantics, ran into "fixed-shape mask" bugs, abandoned the attempt — `references/backends/cuda-graph.md` covers exactly those bugs.

**Process this round, in order:**

1. Read the `serving-systems` skill's `SKILL.md` body (the router) if you haven't already, to know which references exist.
2. For every kernel / library / algorithm named in this round's task, open the corresponding `references/<tier>/<topic>.md`. Skim is fine; cover-to-cover only when the task is structural.
3. **In your `summary` field at the end of the round, name each reference you opened and the specific recommendation from it that shaped your implementation.** If you skipped a reference because you already had recent context on it, say that — but you must say *which* reference and *why*.

If you cannot identify a relevant reference for a task, search the `references/` tree before falling back to priors. The cost of opening one wrong file is tiny; the cost of an unread one is a round of wasted implementation.
