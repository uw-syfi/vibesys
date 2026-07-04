# Domains — pointing vibeserve at your problem space

A **domain** bundles the cross-cutting context the agents need for whatever
you're building: the background knowledge the implementer must read, the
correctness/performance/integrity gates the judge must enforce, and the same for
the single-agent ablation. It's the answer to *"what kind of system is this, and
what does 'good' mean here?"* — kept separate from the neutral prompt skeleton.

Pick one with `--domain` (agent loop):

```bash
vibe-serve --outer-loop agent --domain llm-serving ...      # default
vibe-serve --outer-loop agent --domain generic ...          # no domain context
vibe-serve --outer-loop agent --domain ./my-domain.md ...   # your own (a path)
```

`--domain` accepts either a **built-in name** (a `<name>.md` next to this file)
or a **path** to your own `.md` file anywhere on disk. Built-ins:

| Domain        | What it does |
|---------------|--------------|
| `llm-serving` | The default. LLM inference server context: the `serving-systems` skill/references, `/model` weights, the accuracy + benchmark + reward-hack judge gates. |
| `generic`     | Empty — no domain prose injected. The neutral baseline; copy it to start your own. |

## Anatomy of a domain file

A domain is **one Markdown file**. The injected content lives under `##` headings
named for the agent roles; everything before the first role heading is human
documentation (a title, a "use for…" line) and is ignored by the loop.

```markdown
# My domain
**Use for:** a one-line description of when to reach for this domain.

## implementer        ← injected as {{ domain_implementer }}
What the builder must know / read for this domain.

## judge              ← injected as {{ domain_judge }}
What the reviewer must check for this domain.

## single_agent       ← injected as {{ domain_single_agent }} (optional)
Combined builder+reviewer context for the single-agent ablation.

## orchestrator       ← injected as {{ domain_orchestrator }} (optional)
Planning guidance for the round orchestrator — e.g. the optimization floor it
should establish before chasing workload-specific wins, plus your problem
space's task examples, skill map, and pass-criteria examples.

## roadmap_seed       ← seeds a fresh run's roadmap.md ## Major list (optional)
Starter Major items the orchestrator inherits on round 1, one bullet each.
```

Rules:

- **The heading is the address.** A line that is exactly `## implementer`,
  `## judge`, `## single_agent`, `## orchestrator`, or `## roadmap_seed` starts
  that role's section; it runs until the next role heading. Your section body can
  use its own `##`/`###` sub-headings — only those exact role names delimit a
  section.
- **A missing section injects nothing** for that role.
- **`## single_agent` is optional.** Omit it and it's derived automatically by
  concatenating your `## implementer` and `## judge` sections — no third copy to
  hand-maintain. Add it only when the single-agent ablation needs different
  framing.
- **`## orchestrator` is optional.** Omit it to inject nothing into the planner
  prompt (its neutral skeleton still applies). Add it to give the planner
  domain-specific strategy. The base planner prompt is deliberately neutral — it
  owns only universal loop discipline (roadmap upkeep, task granularity, pass
  criteria) and delegates every problem-space *example* here. Organize the
  section with `###` sub-headings (`### Optimization floor`, `### Task examples`,
  `### Skill map`, `### Pass-criteria examples`, …); `llm-serving` uses them for
  the continuous-batching/attention-kernel/CUDA-graph playbook, and a domain
  author "adjusts a section" by editing those `###` blocks — no code change.
- **`## roadmap_seed` is optional.** Its rendered body is dropped into the fresh
  run's `roadmap.md` under `## Major` (round 1 only), so the planner starts from
  a domain-appropriate arc instead of re-deriving one. Omit it and the roadmap
  seeds a neutral "populate on round 1 based on the objective" placeholder. One
  bullet per starter Major item.
- Write normal Markdown prose. The base template owns the surrounding structure
  (task, pass criteria, workspace, output contract); your section owns the
  domain content.

### Branching on the run (optional Jinja)

Section bodies are rendered with Jinja, so you can branch on the run's context.
Most domains never need this — reach for it only when a gate depends on what's
attached to the run. **Every role section gets the same variables**, so you can
use any of these in any section without tracking which role you're in:

| Variable | Meaning |
|----------|---------|
| `modality` | The `--modality` value (e.g. `text_generation`). |
| `interface` | The `--interface` value: `inprocess` (checker imports the code; Python) or `service` (exercised over the wire; any language). Gate in-process/Python-only requirements with `{% if interface != "service" %}`. |
| `reference_path` | Path to the reference implementation. |
| `bench_path` | Benchmark harness dir, or falsy if no benchmark is attached. |
| `accuracy_checker_path` | Accuracy checker dir, or falsy if not attached. |
| `runtime_notes` | Runtime-environment notes for the round. |

These are always defined (falsy when not applicable), so a plain `{% if bench_path %}`
is enough — no `is defined` guard needed.

Example (inside a `## judge` section):

```jinja
## Correctness gates

1. `pytest` passes.
{% if bench_path %}
2. Run `{{ bench_path }}/benchmark.py` and confirm it succeeds.
{% endif %}
```

## How to author your own

1. Copy `generic.md` to a new file (in-repo `_domain/<name>.md`, or anywhere on
   disk you'll point `--domain` at).
2. Edit the title and "use for…" line at the top.
3. Fill `## implementer` (what to read / what "done" means here) and `## judge`
   (what to check). Leave a section out to inject nothing for that role.
4. Optionally add `## single_agent` for the `--inner-loop single-agent` ablation;
   omit it to derive it from the other two.
5. Run `vibe-serve --outer-loop agent --domain <name-or-path> ...`.

That's it — no code change. A new built-in domain is just a new `.md` file here;
a private domain is just a path you pass.

## Scope

Domains cover **implementer + judge (+ single-agent + orchestrator + roadmap seed)
context**. Two adjacent concerns are deliberately *not* part of a domain file:

- **Language/tooling** (e.g. "use `uv`/`pytest`") is decided by the run's
  `--interface` mode, not the domain: `inprocess` pins Python (uv toolchain +
  in-process `VibeServeModel` contract); `service` leaves the language to the
  agent. It is not a user-facing pack.
- **Profiling** (nsys/torch GPU capture) is selected by `--profiler` and rendered
  by the profiler prompts, not the domain. Domain-specific profiling is future
  work tied to pluggable profilers.
