---
name: write-agent-prompt
description: Write or change the text VibeSys sends to its coding agents, at the layer that owns it, through the template renderer, with the checks and goldens that pin it. Use when editing a strategy's role templates (src/vibesys/orchestration/*/core_policy/prompts/), a domain pack's role files or modality partials (src/vibesys/domains/*/prompts/), backend or run-environment fragments and shared templates (src/vibesys/prompts/), the renderer (src/vibesys/prompts/renderer.py, src/vibesys/domains/rendering.py, libs/vs-prompts), the domain sections in src/vibesys/core_wiring.py, a reply model whose field names templates render, or a prompt golden fixture.
metadata:
  kind: codebase
---

# Write agent prompt

VibeSys drives coding agents in roles (planner, implementer, judge, profiler,
single agent). Each role's prompt is a Jinja template rendered by
`vs_prompts.api.TemplateRenderer`, which raises on any variable the caller did
not pass. Terms: a **strategy** is one search loop under
`src/vibesys/orchestration/`, with templates in its `core_policy/prompts/`; a
**domain pack** is prose for one problem space (`llm-serving`, `database`, ...)
as one `<role>.md` per role under `src/vibesys/domains/<module>/prompts/`,
picked by the input bundle's `[agent].domain`; a **fragment** is a snippet
per hardware backend (`src/vibesys/prompts/backend/`) or run environment
(`src/vibesys/prompts/environments/`); the **task bundle** is the user's
`.vibesys/tasks/<name>/`, whose `OBJECTIVE.md` states the goal and benchmark
contract; a **golden** is a checked-in rendered prompt a test compares byte for
byte. [references/mechanics.md](references/mechanics.md) explains how the
pieces are assembled.

## Builds on

- `software-design`: one source of truth per fact (the reply model owns field
  names, the pack owns domain text); Python computes facts, templates word them.
- `testing`: goldens and paired positive and negative cases. The
  `write-vibesys-tests` skill covers this repository's test harness.

## When this applies

- Any `.j2` template under `src/vibesys/`, `libs/vs-runtime/src/vs_runtime/prompts/`
  or `src/server/chat/prompts/`, or a domain pack `.md` file.
- A new domain role file, modality, backend fragment name or environment note.
- `src/vibesys/prompts/renderer.py`, `src/vibesys/domains/rendering.py`,
  `src/vibesys/core_wiring.py:domain_prompt_sections`, `libs/vs-prompts/`.
- Renaming, adding or removing a field of a strategy's reply model.
- Not product skills under `resources/skills/`; see
  [skill-metadata.md](../../../docs/contributing/skill-metadata.md).

## Steps

1. **Put each fact in the layer that owns it.** Every strategy and domain:
   `src/vibesys/prompts/core_protocol/` or `src/vibesys/prompts/shared/`. One
   strategy: its prompts directory. One problem space: the pack's role file.
   One backend or environment: its fragment. One task: the bundle's
   `OBJECTIVE.md`. A strategy or shared template never tests the run's domain
   (`tests/architecture/test_prompts_name_no_domain.py`), and a pack's text
   reaches only its own runs
   (`tests/vibesys/orchestration/domains/test_domain_sections_in_prompts.py`).
   Task rules kept in a pack are Unenforced: telling a task's contract from a
   domain's needs judgment; ask whether another task in the domain wants it.
2. **Write the words in the template; Python passes data.** No literals, `+`,
   f-strings, `.format`, `.join`, `.replace` or `dedent` on agent-bound text,
   no edits to a render result, no `RenderedPrompt` built outside the renderer
   (`tests/architecture/test_prompt_templates.py`). Example:
   `src/vibesys/core_wiring.py:domain_prompt_sections` passes run facts; the
   pack decides what to say about them.
3. **Pass every variable a template reads, and read every one you pass.** A
   missing one raises (`libs/vs-prompts/tests/test_renderer.py`). A new
   variable joins
   `tests/architecture/test_prompt_resource_contract.py:representative_context`
   (or `core_policy_context`). One passed but deliberately unread carries
   `{# vs-prompts:unused: <name> - <reason> #}`, as in
   `src/vibesys/prompts/shared/profilers/otel.j2`; environment templates are
   held to it by
   `tests/vibesys/sandbox/test_environment_prompt_snapshots.py:test_environment_templates_use_every_kwarg_their_call_site_passes`.
4. **Name reply fields only through `reply`:** `{{ reply.parent_hypothesis_id }}`,
   never the backticked name (`tests/architecture/test_prompt_reply_fields.py`).
   `reply` holds only fields the run offers, so naming another raises
   `vs_prompts.api.UnknownReplyFieldError`. A field's meaning goes in its
   Pydantic `description`, which the agent also receives.
5. **Domain text goes in role files** that every strategy prints in one slot
   (`src/vibesys/prompts/core_protocol/domain_section.j2`). All role files get
   the variables listed in
   `src/vibesys/domains/rendering.py:render_domain_section`. A modality is
   declared on `src/vibesys/domains/base.py:DomainDefinition` with one partial
   per role that the role file includes
   (`tests/vibesys/orchestration/domains/test_serving_modality.py`).
6. **A backend fragment exists for every backend:** add the name to
   `src/vibesys/prompts/renderer.py:ComputeBackendFragment.NAMES` and a
   `<name>.j2` (empty means a deliberate skip) under each backend directory.
   `BackendPromptRenderer` validates this when constructed
   (`tests/vibesys/test_backend_fragments.py`). Read it as `{{ <name> }}`.
7. **Every strategy shows the environment notes** through `runtime_notes`,
   `environment_notes` or `core_protocol/run_context.j2`
   (`tests/architecture/test_environment_notes_in_prompts.py`).
8. **Name only paths and tools the agent has.** Cited paths must exist in its
   workspace or mounts
   (`tests/architecture/test_prompt_resource_contract.py:assert_resource_citations`);
   only offered profiler tools may be named
   (`tests/vibesys/orchestration/test_profiler_tool_offer.py`).
9. **Guard a domain's distinctive vocabulary** by adding its terms to
   `tests/vibesys/orchestration/domains/test_prompt_contracts.py:DOMAIN_LEAK_CHECKS`.

## Testing

- With `uv run pytest`, run `tests/architecture/test_prompt_templates.py`,
  `tests/architecture/test_prompt_reply_fields.py`,
  `tests/architecture/test_prompts_name_no_domain.py`,
  `tests/vibesys/orchestration/domains/` and `libs/vs-prompts/tests/`, then
  the goldens covering what you changed.
- Regenerate a golden only for an intended change and read its diff as the
  prompt diff; [references/goldens.md](references/goldens.md) maps goldens to
  tests and variables (`tests/vibesys/golden/helpers.py:assert_exact_text`).
  Unenforced: only a reviewer can tell an intended change from a regression.
- Render in tests as production does, with the `reply` variables the role
  offers (`tests/vibesys/reply_variables.py:context_variables`).
- Before a PR run `tests/architecture/test_prompt_resource_contract.py`: it
  renders every template from the built wheel, so it also catches a prompt
  file the `pyproject.toml` package-data globs miss.

## Known failure modes

- Serving modality partials in shared prompts reached non-serving runs and
  missed three strategies (#2146):
  `tests/vibesys/orchestration/domains/test_serving_modality.py:test_modality_text_never_reaches_a_generic_run`.
- The pack reached only some strategies' role prompts (#2141):
  `tests/vibesys/orchestration/domains/test_domain_sections_in_prompts.py`.
- Issue-queue templates still test `facts.domain_id == "llm-serving"`, the
  exact-count exemptions in `tests/architecture/test_prompts_name_no_domain.py`.
  Move that text into the pack and lower the count; never add an entry.
- Typed reply field names drifted from the schema (#2216):
  `tests/architecture/test_prompt_reply_fields.py`.
- A new template variable broke the packaged render until the representative
  context learned it (#2133, #2141):
  `tests/architecture/test_prompt_resource_contract.py:core_policy_context`.
- A new strategy forgot the environment notes, so live Slurm agents never
  learned how commands run: `tests/architecture/test_environment_notes_in_prompts.py`.
- Prompts named profiler tools that fail without an editor GPU (#2185, #2188):
  `tests/vibesys/orchestration/test_profiler_tool_offer.py`.
- A profiler prompt printed an absent benchmark command (#2128):
  `tests/vibesys/orchestration/test_profiler_templates.py`.
- CUDA and ROCm `profiling_workflow.j2` fragments carry serving steps
  (FlashInfer, `/v1/completions`) that every domain on that backend reads.
  Unenforced: telling backend mechanics from serving prose needs judgment; put
  new serving steps in the pack's `profiler.md`.
- Editing `tests/vibesys/golden/snapshots/`, which no test reads, and taking a
  green suite as coverage. Unenforced: no check maps fixtures to their readers.

## Sources of truth

- `libs/vs-prompts/README.md`: strict rendering, contracts, reply fields.
- `src/vibesys/prompts/renderer.py`: template roots, backend fragment names.
- `src/vibesys/domains/base.py:DomainRole`, `src/vibesys/domains/registry.py:DOMAINS`.
- [domains.md](../../../docs/contributing/domains.md): pack anatomy and role
  file variables. It and `src/vibesys/prompts/backend/README.md` are partly
  stale; [references/mechanics.md](references/mechanics.md) lists where.
