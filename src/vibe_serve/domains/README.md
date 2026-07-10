# Domains — pointing vibeserve at your problem space

A **domain** bundles the cross-cutting context the agents need for whatever
you're building: the background knowledge the implementer must read, the
correctness/performance/integrity gates the judge must enforce, profiling
workflow details, and the same for the single-agent ablation. It's the answer to
*"what kind of system is this, and what does 'good' mean here?"* — kept separate
from the neutral prompt skeleton.

Pick one with `--domain` (agent loop):

```bash
vibe-serve --outer-loop agent --domain llm-serving ...      # default
vibe-serve --outer-loop agent --domain generic ...          # no domain context
```

`--domain` accepts a registered repo domain name:

| Domain        | What it does |
|---------------|--------------|
| `llm-serving` | The default. LLM inference server context: the `serving-systems` skill/references, `/model` weights, the accuracy + benchmark + reward-hack judge gates. |
| `generic`     | Empty — no domain prose injected. The neutral baseline; copy it to start your own. |

## Anatomy of a domain package

Each domain owns one package folder. Prompt content lives under `templates/` in
Markdown files named for the agent roles. Domain-specific environment
setup/teardown code lives next to the templates when the domain needs it.

```text
src/vibe_serve/domains/my_domain/
  __init__.py       # exports DEFINITION
  hooks.py          # optional domain-specific EnvironmentHooks implementation
  templates/
    README.md        # optional human documentation
    implementer.md   # injected as {{ domain_implementer }}
    judge.md         # injected as {{ domain_judge }}
    single_agent.md  # injected as {{ domain_single_agent }} (optional)
    orchestrator.md  # injected as {{ domain_orchestrator }} (optional)
    profiler.md      # injected as {{ domain_profiler }} (optional)
```

Rules:

- **Inside `templates/`, the filename is the address.** `implementer.md` maps to
  `{{ domain_implementer }}`, `judge.md` maps to `{{ domain_judge }}`, and so on.
- **A missing role file injects nothing** for that role.
- **`single_agent.md` is optional.** Omit it and it's derived automatically by
  concatenating `implementer.md` and `judge.md` — no third copy to hand-maintain.
  Add it only when the single-agent ablation needs different framing.
- **`orchestrator.md` is optional.** Omit it to inject nothing into the planner
  prompt (its neutral skeleton still applies). Add it to give the planner
  domain-specific strategy — `llm-serving` uses it for the
  continuous-batching/attention-kernel/CUDA-graph optimization floor.
- **`profiler.md` is optional.** Omit it to use only the selected profiler's
  neutral mechanics. Add it when the domain needs a specific capture recipe,
  server startup contract, benchmark shape, or remote profiling entry point.
- Write normal Markdown prose. The base template owns the surrounding structure
  (task, pass criteria, workspace, output contract); your role file owns the
  domain content.

### Branching on the run (optional Jinja)

Role files are rendered with Jinja, so you can branch on the run's context.
Most domains never need this — reach for it only when a gate depends on what's
attached to the run. **Every role file gets the same variables**, so you can
use any of these in any file without tracking which role you're in:

| Variable | Meaning |
|----------|---------|
| `modality` | The `--modality` value (e.g. `text_generation`). |
| `interface` | The `--interface` value: `inprocess` (checker imports the code; Python), `service` (over the wire), or `native` (manifest commands load a native artifact). Gate Python-only requirements with `{% if interface == "inprocess" %}`. |
| `reference_path` | Path to the reference implementation. |
| `benchmark_command` | Benchmark command declared by the input manifest, or falsy if no benchmark is attached. |
| `accuracy_command` | Accuracy-checker command declared by the input manifest, or falsy if not attached. |
| `runtime_notes` | Runtime-environment notes for the round. |

These are always defined (falsy when not applicable), so a plain `{% if benchmark_command %}`
is enough — no `is defined` guard needed.

Example (inside `judge.md`):

```jinja
## Correctness gates

1. `pytest` passes.
{% if benchmark_command %}
2. Run `{{ benchmark_command }}` and confirm it succeeds.
{% endif %}
```

## How to add a domain

1. Copy `generic/` to a new in-repo `src/vibe_serve/domains/<module_name>/`
   package, using underscores for the Python module name when the CLI domain
   name contains hyphens.
2. Edit `templates/README.md` with the title and "use for…" line.
3. Add `implementer.md` (what to read / what "done" means here) and `judge.md`
   (what to check) under `templates/`. Leave a file out to inject nothing for
   that role.
4. Optionally add `single_agent.md` for the `--inner-loop single-agent`
   ablation; omit it to derive it from the other two.
5. Optionally add `orchestrator.md` and `profiler.md` when the neutral planning
   or profiling skeleton needs domain-specific examples or capture commands.
6. Export a `DEFINITION` from the domain package's `__init__.py`. If the domain
   needs setup/teardown behavior such as mounts or copy exclusions, implement
   `EnvironmentHooks` in that package and attach it to the definition.
7. Register the definition in `vibe_serve.domains.registry.DOMAINS`.
8. Run `vibe-serve --outer-loop agent --domain <name> ...`.

Domains are registered explicitly so prompt context, environment hooks, and tests
stay tied to the same domain identity.

## Scope

Domains cover **implementer + judge + profiler (+ single-agent + orchestrator) context**.
One adjacent concern is deliberately *not* part of a domain package:

- **Language/tooling** (e.g. "use `uv`/`pytest`") is decided by the run's
  `--interface` mode, not the domain: `inprocess` pins Python (uv toolchain +
  the in-process accuracy-checker contract); `service` leaves the language to
  the agent. It is not a user-facing pack.
