# CLAUDE.md — authoring guide for vibe-serve-skills

This file is loaded automatically when Claude works inside this repo. It captures the conventions every reference document here should follow, so new / edited content stays coherent with the collection.

## Layout: one skill, many references organized by tier

This collection follows the [agentskills.io](https://agentskills.io/specification) **single-skill + references** pattern, with references grouped into tier subdirectories for browsability:

```
skills/serving-systems/
├── SKILL.md                   # the only skill — keep tiny; routes to references/
├── references/
│   ├── algorithms/            # serving algorithms
│   │   ├── <topic>.md         # main note for the topic
│   │   └── <topic>-<sub>.md   # follow-up details (main file links to it)
│   ├── backends/              # kernel-library backends (FlashInfer, FA, …)
│   ├── engines/               # vLLM / SGLang / TensorRT-LLM source maps
│   ├── frameworks/            # PyTorch / Triton / MLX
│   ├── hardware/              # NVIDIA / AMD / Apple specifics
│   ├── models/                # model-architecture notes
│   └── tooling/               # OpenAI API / profiler / benchmark / etc.
├── repos/                     # vLLM / SGLang / TensorRT-LLM submodules
│                              # (excluded from agent materialization)
└── README.md, OVERVIEW.md, CLAUDE.md, update-repos.sh   # repo docs
```

Why this shape:

- The agentskills spec loads every skill's `name + description` at startup. With ~50 topics, having each as its own skill burns ~5k tokens of always-loaded context. As one skill with references, only ~360 chars of `description` lives in the always-loaded pool; the body and reference files are read on demand.
- Within `references/`, content is grouped one level deep by tier so the source tree is browsable and the index in SKILL.md mirrors the on-disk layout. Sub-references for one topic (e.g. `cuda-graph-runner.md` for `cuda-graph.md`) sit flat next to the main file inside the same tier dir — never a third level of nesting.

## SKILL.md (the router)

### Frontmatter

```yaml
---
name: serving-systems
description: >-
  ~250-400 characters. Lead with what this covers, then list trigger
  keywords. Hard cap is 1024 chars per the agentskills spec, but stay
  near 100 tokens — this field is in the always-loaded metadata pool.
---
```

Don't add other frontmatter fields (`license`, `compatibility`, `metadata`, `allowed-tools`, …) unless the spec genuinely requires them.

### Body

Keep under **300 lines**. Body sections in order:

1. One-paragraph statement of what the skill bundles.
2. **How to use this skill** — concise instructions on opening a specific reference vs preloading.
3. **Default-on optimizations** — the optimization-floor recommendations (continuous batching, fused attention, CUDA graphs) with links to the relevant references.
4. **Reference index** — every `references/<topic>.md` listed under its tier heading, each entry one line: `- [\`references/<topic>.md\`](references/<topic>.md) — <one-line trigger>.`
5. **Out of scope** — pointers to other skill collections (e.g. agent-gpu-skills for kernel writing).
6. **Reference repos** — `$SERVE_REPOS` placeholder explanation.

The body's job is *only* to route. All technical content lives in `references/`.

## references/ files

### Naming and location

- Topic notes live at `references/<tier>/<topic>.md`, where `<tier>` is one of `algorithms`, `backends`, `engines`, `frameworks`, `hardware`, `models`, `tooling`.
- For follow-up depth on one topic, use `<topic>-<sub>.md` flat **inside the same tier dir** — e.g. `references/backends/cuda-graph.md` + `references/backends/cuda-graph-runner.md`. The main `<topic>.md` must link to its sub-files explicitly. Don't introduce a third nesting level.
- Name files by what they *contain*, not by section number (`paged-kv-cache.md`, not `design-1.md`).

### Body

- Under **500 lines** per file. Split into a follow-up `<topic>-<sub>.md` if longer.
- Start with a `# <Topic>` H1.
- Conventional section order:
  1. One-line purpose.
  2. Prerequisites (what the caller must already have).
  3. Concept / design (brief — link to follow-up files for depth).
  4. Workflow / main pattern (code outlines, pseudo-code, checklists).
  5. Compatibility matrix or "Where's X" table where it fits the topic.
  6. Pitfalls (non-obvious gotchas).
  7. Additional references (links to follow-ups + external docs).
- No YAML frontmatter on reference files — they're loaded via explicit Read calls from SKILL.md, not by skill discovery.

### Cross-references

When a `references/<topic>.md` benefits from a compatibility matrix or a "where's X" table, include it in-file. Cross-link to other reference files freely with relative paths from the skill root, e.g. `[backends/cuda-graph](references/backends/cuda-graph.md)`.

### Engine source-map references

Files under the `engines` tier (e.g. `references/vllm.md`, `references/sglang.md`, `references/trtllm.md`) include a **"Where's X" table**:

```markdown
| Need | Path in repos/<engine>/ |
|:-----|:------------------------|
| Attention backends | python/sglang/srt/layers/attention/ |
| Scheduler | python/sglang/srt/managers/scheduler.py |
```

### Backend / kernel-library references

Files under the `backends` tier end with:

```markdown
## Out of scope — kernel implementation

For writing new kernels (not using this library): see agent-gpu-skills's
triton-skill / cutlass-skill / cuda-skill.
```

### Algorithm references

Files under the `algorithms` tier include a compatibility matrix near the end:

```markdown
## Compatibility

| Implementation | Engine | Backend / library | Hardware |
|:--|:--|:--|:--|
| FlashInfer paged KV attention | SGLang, vLLM | flashinfer | NVIDIA (sm_80+) |
| FA3 variable-length | vLLM v1 | flashattention | NVIDIA Hopper+ |
```

This is how axis-crossing knowledge lives — not in the directory tree.

## Reference-repo path convention

Repos live at `skills/serving-systems/repos/{vllm,sglang,TensorRT-LLM}/` (git submodules). Reference files cite paths via:

```
$SERVE_REPOS = <vibe-serve-root>/skills/serving-systems/repos
```

Example grep recipe:

```bash
rg "register.*backend" $SERVE_REPOS/vllm/vllm/v1/attention/backends/
```

Tell the reader to export `SERVE_REPOS=$(git rev-parse --show-toplevel)/skills/serving-systems/repos` or substitute inline.

The `repos/` directory is **excluded** from agent materialization (see `vibeserve_agent/agents/cli_runner.py::_materialize_skills`); reference paths into it are advisory grep recipes, not runtime imports.

## What not to include

- **No frontmatter on `references/**/*.md` files.** They're not skills; they're follow-up reading.
- **No third nesting level inside `references/`.** One tier subdir is the limit; sub-references live flat next to the main file with `<topic>-<sub>.md` naming.
- **No tier subdirectories with their own `SKILL.md`.** The single top-level SKILL.md is the only skill.
- **No emojis** unless the user explicitly asks.
- **No kernel-implementation details.** Link to agent-gpu-skills instead.

## Adding a topic

1. Decide which tier it belongs to (models / algorithms / backends / frameworks / hardware / engines / tooling).
2. Create `references/<tier>/<topic>.md` with the body conventions above. No frontmatter.
3. Edit `SKILL.md`'s "Reference index" section to add a one-line entry under the right tier heading. The link path must be `references/<tier>/<topic>.md`.
4. If the topic crosses axes, update the compatibility matrix in the relevant `references/algorithms/<algorithm>.md`.

## Editing a topic

- The router's description in `SKILL.md` triggers loading. If a new topic introduces a keyword the description doesn't already match, add it (sparingly — keep ≤400 chars).
- Reference files can grow up to ~500 lines; if longer, split into `<topic>-<sub>.md` and link from the main file.

## Running the reference repos

```bash
git submodule update --init skills/serving-systems/repos       # initialize all
git submodule update --init skills/serving-systems/repos/vllm  # initialize one
git -C skills/serving-systems/repos/vllm pull origin main      # update one
```

`update-repos.sh` is the upstream sparse-checkout helper; here the repos are tracked as shallow git submodules instead.

## Style

- **Imperative / infinitive voice** in instructions ("Reshape to NHD", not "You should reshape to NHD").
- **Concise code blocks** over prose explanations.
- **Tables for enumerations** — faster to scan than bullet lists for both Claude and humans.
- **No "I will ..." / "Let me ..."** — references aren't first-person.
