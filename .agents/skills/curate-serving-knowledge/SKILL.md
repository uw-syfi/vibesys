---
name: curate-serving-knowledge
description: Promote findings from an optimization campaign's ledger into the serving-systems skill tree under resources/skills/serving-systems, and keep that tree correct over time. Use when a campaign phase ends, a PR stack lands, a blocker's fix has been reproduced, a documented fact turns out wrong, or a campaign starts on a newer image than the tree's entries were verified on. Covers the promotion test, scope and granularity classification, entry formats by type, placement rules, freshness passes, the validator, and delivery as a PR.
---

# Curate serving knowledge

The knowledge tree records conclusions; the campaign ledger records how they were reached. This skill moves conclusions from the ledger into the tree in a form the next campaign can find and trust, and removes conclusions that stopped being true.

Read `resources/skills/serving-systems/CLAUDE.md` first. It defines the tree layout, the portability classes A, B, and C, the link discipline, and the file conventions. This skill adds the rules for what gets in, at what scope, and in what shape.

## Inputs

- The ledger at `.vibesys/tasks/<task>/LEDGER.md` in the task repository (format defined by the `optimization-loop` skill).
- The current tree under `resources/skills/serving-systems/references/`.
- The task's platform config (for example `config/platforms/<name>.toml` next to the harness) for the scope vocabulary and the executable recipe the floor must agree with.

## Triggers

| Trigger | Action |
|:--|:--|
| A PR stack lands or a campaign phase ends | Full promotion pass over the ledger since the last pass |
| A blocker's fix is reproduced a second time | Promote that one entry now |
| A documented fact is found wrong | Correct or delete it now, in place |
| A campaign starts on a newer image or engine than the entries' stamps | Freshness pass before the first boot |

## Promotion test

An entry enters the tree only if one of these holds:

- Reproduced at least twice (two nodes, two days, or two boots with the same outcome).
- Observed once and explained by a mechanism read in source (cite the file and symbol).
- A public specification (hardware numbers, documented API behavior).

Everything else stays in the ledger. If an observation is valuable but fails the test, it may enter as `candidate` (below) only when the mechanism is at least plausible and the entry says what would verify it.

## Scope and granularity

Tag every entry with the narrowest scope at which it is known to hold. Vocabulary, from wide to narrow:

| Scope | Example values | Meaning |
|:--|:--|:--|
| backend | `rocm`, `cuda` | The `ComputeBackend` name; same as the platform directory |
| gfx or sm target | `gfx942`, `gfx950`, `sm_90` | ISA generation; where kernel availability is decided |
| SKU | `MI300A`, `MI300X` | Specific part; memory size, form factor |
| property | `unified-memory`, `discrete-memory` | Physical property that generalizes across SKUs and vendors |
| software | `sglang-v0.5.18-rocm700`, `aiter <version>` | Image, engine, or library version the fact was observed under |

Rules:

- Use the same names the task's platform config uses, so a finding and the config that encodes it cross-reference by string.
- Prefer a property over a SKU when the mechanism is the property. "Under unified memory, page cache competes with weights" generalizes; "on MI300A, page cache competes with weights" does not.
- A fact that names a path, a scheduler, a filesystem, or a node id is a site fact. Reject it from the tree and point it at the task's site config.
- Software scope is mandatory for anything a version bump could change: kernel availability, default flag values, JIT behavior, timeouts.

## Entry status

Every entry carries a status:

- `verified`: passed the promotion test.
- `candidate`: mechanism plausible, not reproduced; the entry says what would verify it.

Readers treat candidates as hypotheses. Flip a candidate to verified when the second reproduction appears in a later ledger; delete it when refuted.

## Entry formats by type

Each type has one home and one shape. Do not write prose paragraphs where a table row belongs.

| Type | Home | Shape |
|:--|:--|:--|
| capability | kernel-library file under `platforms/<backend>/` | matrix row keyed by gfx or sm target |
| quantity | `platforms/<backend>/hardware.md` | table row with value, scope, stamp |
| recipe | `platforms/<backend>/floor.md` | one line per flag or env var, linking to the rationale |
| procedure | the topic file, workflow section | numbered steps |
| pitfall | the topic file, pitfalls section | symptom first, then cause, fix, scope, stamp |
| contract exception | the portable contract's compatibility matrix | N/A row keyed by property |
| model fact | `references/models/<model>.md` | structure table, sharding notes, known gates |

Pitfalls are symptom-first because the next reader arrives with an error string and greps for it:

```
Symptom: server exits on the first request; log shows a JIT build of mha_batch_prefill
         followed by a health-check failure.
Cause:   AITER builds kernel variants lazily; SGLANG_HEALTH_CHECK_TIMEOUT defaults to 20 s.
Fix:     set SGLANG_HEALTH_CHECK_TIMEOUT=1800; treat the first request after boot as warmup.
Scope:   rocm, sglang 0.5.x with aiter.
Status:  verified. sglang-v0.5.18-rocm700, 2026-09-05, job 623402.
```

Quote the literal log text where one exists. Include the fix's command or flag, not a description of it.

Every entry ends with a stamp: software version, date, and one evidence pointer (job id, PR, or ledger row). The evidence itself stays in the ledger.

## Placement rules

- `floor.md` is a router: the recipe, and one line per known pitfall linking to the file that holds the detail. If the floor exceeds about a screen, move detail down, do not trim the index.
- Detail goes to the existing topic file when one fits; otherwise a new flat file `platforms/<backend>/<topic>.md`, with a one-line index entry added to the collection's `SKILL.md`.
- A finding that changes a portable contract gets an N/A row in that contract's matrix, keyed by property. Never deep-link from a portable file into `platforms/<backend>/`.
- Class C content (a library, an ISA, a form factor) never leaves `platforms/<backend>/`. If a sentence about an APU appears in a portable file, it is misplaced.
- When a fact is wrong: correct or delete in place. Do not append caveats to a wrong sentence.

## Freshness pass

At the start of a campaign on a newer image, engine, or library than an entry's stamp:

1. List entries under the target `platforms/<backend>/` and the model file whose software scope predates the new version.
2. For each: re-verify on the first boot if cheap, else mark `candidate` with the old stamp until the campaign reproduces or refutes it.
3. Re-stamp what held; correct or delete what did not.

Capability rows are the most likely to change (a missing kernel path appears in a release). Quantities from public specs do not need re-verification.

## Procedure for a promotion pass

1. Read the ledger rows since the last pass. Group by finding, not by experiment.
2. Apply the promotion test to each finding. Set status.
3. Classify A, B, or C per `CLAUDE.md`, then assign scope and type.
4. Reject site facts; note them for the task's site config if not already there.
5. Write or update entries in the formats above. Check the floor still agrees with the task's platform config.
6. Run the checks below.
7. Open a PR to the VibeSys repository with the `open-pr` skill. The PR body lists each promoted entry with its status and stamp; a reviewer sees the promoted set once, in batch.

## Checks

- Link discipline: no portable file links into `platforms/`. `grep -rn "](../platforms/[a-z]*/" resources/skills/serving-systems/references/{algorithms,models,tooling,frameworks}` must return nothing.
- Every new or edited entry has a scope, a status, and a stamp.
- No site facts: `grep -rn "capstor\|/scratch/\|nid[0-9]\|--account\|sbatch" resources/skills/serving-systems/references` must return nothing.
- `uv run pytest tests/entrypoints/test_skills_wiring.py -q` passes (the skill-tree validator runs there).
- Files stay under 500 lines; split into `<topic>-<sub>.md` if not.

## Pitfalls of curation

- **Writing rules from one observation.** "Loader threads above 2 crash" holds under one memory configuration and one checkpoint size. Write the observation with its conditions, or write nothing.
- **Site facts dressed as hardware facts.** A storage read cap feels like a hardware number for a day. Ask whether the claim survives moving the node to a different filesystem.
- **Unstamped entries.** An entry with no software scope cannot be aged out and will mislead the first campaign after a version bump.
- **Floor bloat.** Every line added to the floor is read on every run of every campaign on that backend.
- **Caveat stacking.** A wrong sentence with three appended exceptions is still wrong. Rewrite it.
- **Curating from memory.** Promote from the ledger, where predictions and measurements sit side by side, not from recollection of what seemed true.
