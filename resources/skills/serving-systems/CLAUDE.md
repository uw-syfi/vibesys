# CLAUDE.md — authoring guide for vibesys-skills

This file is loaded automatically when Claude works inside this repo. It captures the conventions every reference document here should follow, so new / edited content stays coherent with the collection.

## Layout: one skill, many references organized by tier

This collection follows the [agentskills.io](https://agentskills.io/specification) **single-skill + references** pattern, with references grouped into tier subdirectories for browsability:

```
skills/serving-systems/
├── SKILL.md                   # the only skill — keep tiny; routes to references/
├── references/
│   ├── algorithms/            # portable serving-algorithm contracts
│   │   ├── <topic>.md         # main note for the topic
│   │   └── <topic>-<sub>.md   # follow-up details (main file links to it)
│   ├── engines/               # vLLM / SGLang / TensorRT-LLM source maps
│   ├── frameworks/            # cross-platform only: PyTorch / Triton
│   ├── models/                # model-architecture notes
│   ├── tooling/               # OpenAI API / benchmark / IO / etc.
│   └── platforms/             # ONE dir per ComputeBackend value
│       ├── cuda/              #   floor.md + hardware.md + profiler.md
│       ├── rocm/              #   + that platform's kernel/framework notes
│       ├── trainium/
│       ├── metal/
│       └── cpu/
├── repos/                     # vLLM / SGLang / TensorRT-LLM submodules
│                              # (excluded from agent materialization)
└── README.md, OVERVIEW.md, CLAUDE.md, update-repos.sh   # repo docs
```

Why this shape:

- The agentskills spec loads every skill's `name + description` at startup. With ~50 topics, having each as its own skill burns ~5k tokens of always-loaded context. As one skill with references, only ~360 chars of `description` lives in the always-loaded pool; the body and reference files are read on demand.
- Within `references/`, content is grouped one level deep by tier so the source tree is browsable and the index in SKILL.md mirrors the on-disk layout. Sub-references for one topic (e.g. `cuda-graph-runner.md` for `cuda-graph.md`) sit flat next to the main file inside the same tier dir.
- `references/platforms/<backend>/` is the **single exception** to the one-level rule. Directory names must be exact `ComputeBackend` values (`cuda`, not `nvidia`) because `_materialize_skills` prunes foreign platforms by literal name match, and `validate_skill_tree` rejects anything else.

## Portability: the A/B/C rule

Every reference falls into one of three classes. Getting this wrong is the main way this collection goes bad, because a platform-specific claim in a portable file is *wrong work* on other backends, not merely noise.

| Class | What | Where it lives |
|:--|:--|:--|
| **A — portable** | Concept + compatibility matrix. Platform-specificity confined to table cells. | `algorithms/`, `models/`, `tooling/`, `frameworks/` — one copy |
| **B — portable question, divergent answer** | The problem generalizes; the technique forks per backend. | Contract in the portable tier + `platforms/<backend>/<topic>.md` per backend |
| **C — platform-only** | A specific library, tool, or ISA. | `platforms/<backend>/` only |

The test for B vs A: **would following this file on another backend produce wrong work?** Not "does it mention CUDA" — grepping for vendor strings misclassifies. `continuous-batching` teaches "eliminate padding", which inverts on Trainium where bucketed padding is required; that is B. `radix-prefix-caching` has a GPU→CPU→NVMe tier ladder that is merely inapplicable under unified memory; that is A with a scope note.

Scale the response to severity:

| Situation | Action |
|:--|:--|
| Platform named in a table cell | Add a row, including explicit **N/A** rows |
| Section inapplicable but harmless | Scope note in place ("Applies to: cuda, rocm") |
| Advice that is **wrong** elsewhere | Fork: contract + `platforms/<backend>/<topic>.md` |

Only write a platform variant where that backend has a real answer. A missing file is honest signal; `validate_skill_tree` surfaces skeleton gaps.

## Link discipline (required — pruning depends on it)

Only files in the portable tiers may be linked from other portable tiers. **A portable file must never markdown-link into `platforms/<backend>/`**, because materialization prunes every non-selected platform and the link would dangle.

- Portable → portable: normal relative links.
- Portable → platform: link the directory (`[platforms/](../platforms/)`) or name the library as plain text. Never `](../platforms/cuda/flashinfer.md)`.
- Contract → its own platform implementations: **name the backends in a table, link only the `platforms/` directory.** A contract is a portable file too, so the same ban applies — a dispatch table that deep-links `](../platforms/cuda/x.md)` dangles on every other backend and the test rejects it.
- Within one `platforms/<backend>/`: normal relative links (both endpoints survive together).

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
4. **Reference index** — every portable-tier file listed under its tier heading, each entry one line: `- [\`references/<tier>/<topic>.md\`](references/<tier>/<topic>.md) — <one-line trigger>.` **`platforms/` is indexed as a directory, not per-file**, because only one backend survives materialization and per-file entries for absent backends would be noise.
5. **Out of scope** — pointers to other skill collections (e.g. agent-gpu-skills for kernel writing).
6. **Reference repos** — `$SERVE_REPOS` placeholder explanation.

The body's job is *only* to route. All technical content lives in `references/`.

## references/ files

### Naming and location

- Topic notes live at `references/<tier>/<topic>.md`, where `<tier>` is one of `algorithms`, `engines`, `frameworks`, `models`, `tooling`, or `platforms/<backend>`.
- For follow-up depth on one topic, use `<topic>-<sub>.md` flat **inside the same tier dir** — e.g. `references/platforms/cuda/cuda-graph.md` + `references/platforms/cuda/cuda-graph-runner.md`. The main `<topic>.md` must link to its sub-files explicitly.
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

When a `references/<topic>.md` benefits from a compatibility matrix or a "where's X" table, include it in-file. Cross-link to other reference files with relative paths, subject to the link discipline above.

### Engine source-map references

Files under the `engines` tier (`references/engines/vllm.md`, `references/engines/sglang.md`, `references/engines/trtllm.md`) include a **"Where's X" table**:

```markdown
| Need | Path in repos/<engine>/ |
|:-----|:------------------------|
| Attention backends | python/sglang/srt/layers/attention/ |
| Scheduler | python/sglang/srt/managers/scheduler.py |
```

### Kernel-library references

Files documenting a kernel library (under `platforms/<backend>/`) end with:

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
| FlashInfer paged KV attention | SGLang, vLLM | flashinfer | `cuda` (sm_80+) |
| FA3 variable-length | vLLM v1 | flashattention | `cuda` (Hopper+) |
| Resident aliased KV buffers | — | NxD | `trainium` — **N/A**, not paged; see `nxd-kv-cache.md` |
```

The hardware column uses exact `ComputeBackend` values so the mapping is greppable. **Include N/A rows** — "this does not apply here, use X instead" is as useful as a positive entry, and its absence is what lets a backend silently inherit another's guidance.

This is how axis-crossing knowledge lives — not in the directory tree.

## Reference-repo path convention

Repos live at `resources/skills/serving-systems/repos/{vllm,sglang,TensorRT-LLM}/` (git submodules). Reference files cite paths via:

```
$SERVE_REPOS = <vibesys-root>/resources/skills/serving-systems/repos
```

Example grep recipe:

```bash
rg "register.*backend" $SERVE_REPOS/vllm/vllm/v1/attention/backends/
```

Tell the reader to export `SERVE_REPOS=$(git rev-parse --show-toplevel)/resources/skills/serving-systems/repos` or substitute inline.

The `repos/` directory is **excluded** from agent materialization (see `src/vibesys/agents/cli_common.py::materialize_skills`); reference paths into it are advisory grep recipes, not runtime imports.

## What not to include

- **No frontmatter on `references/**/*.md` files.** They're not skills; they're follow-up reading.
- **No third nesting level inside `references/`**, except `platforms/<backend>/`. Sub-references live flat next to the main file with `<topic>-<sub>.md` naming.
- **No vendor names for platform directories.** `cuda`/`rocm`, never `nvidia`/`amd` — validation rejects them.
- **No tier subdirectories with their own `SKILL.md`.** The single top-level SKILL.md is the only skill.
- **No emojis** unless the user explicitly asks.
- **No kernel-implementation details.** Link to agent-gpu-skills instead.

## Adding a topic

1. Classify it A / B / C using the rule above.
2. Decide the tier (models / algorithms / frameworks / engines / tooling, or `platforms/<backend>`).
3. Create the file with the body conventions above. No frontmatter.
4. Edit `SKILL.md`'s "Reference index" to add a one-line entry under the right heading.
5. If the topic crosses axes, update the compatibility matrix in the relevant `references/algorithms/<algorithm>.md` — including N/A rows for backends where it does not apply.

## Adding a platform

1. Add the variant to `ComputeBackend` in `vibesys/constants.py` and wire the runtime impl + prompt fragments (see `vibesys/prompts/backend/README.md`).
2. Create `references/platforms/<backend>/` with the full skeleton: `floor.md`, `hardware.md`, `profiler.md`. `validate_skill_tree` fails the run if any is missing.
3. Add platform rows to the compatibility matrices in `algorithms/`, using explicit N/A where a technique does not apply.
4. Add `platforms/<backend>/<topic>.md` only for category-B topics where this backend has a genuinely different answer.

## Editing a topic

- The router's description in `SKILL.md` triggers loading. If a new topic introduces a keyword the description doesn't already match, add it (sparingly — keep ≤400 chars).
- Reference files can grow up to ~500 lines; if longer, split into `<topic>-<sub>.md` and link from the main file.

## Running the reference repos

```bash
git submodule update --init --checkout resources/skills/serving-systems/repos       # initialize all
git submodule update --init --checkout resources/skills/serving-systems/repos/vllm  # initialize one
git -C resources/skills/serving-systems/repos/vllm pull origin main      # update one
```

`update-repos.sh` is the upstream sparse-checkout helper; here the repos are tracked as shallow git submodules instead.

## Style

- **Imperative / infinitive voice** in instructions ("Reshape to NHD", not "You should reshape to NHD").
- **Concise code blocks** over prose explanations.
- **Tables for enumerations** — faster to scan than bullet lists for both Claude and humans.
- **No "I will ..." / "Let me ..."** — references aren't first-person.
