---
name: write-agent-prompt
description: Write or change the text VibeSys sends to its coding agents, at the layer that owns it, through the template renderer, with the checks and goldens that pin it. Use when editing a strategy's role templates, a domain pack's role files or modality partials, backend or run-environment prompt fragments, shared prompt templates, the prompt renderer or the vs-prompts library, the code that builds a run's domain sections, a reply model whose field names templates render, or a prompt golden fixture.
metadata:
  kind: codebase
---

# Write agent prompt

VibeSys drives coding agents in roles (planner, implementer, judge, profiler,
single agent). Each role's prompt is a Jinja template rendered by
`vs_prompts.api.TemplateRenderer`, which raises on any variable the caller did
not pass. Terms: a **strategy** is one search loop, with its own role
templates; a **domain pack** is prose for one problem space as one file per
role, picked by the input bundle's `[agent].domain`; a **fragment** is a
snippet per hardware backend or run environment; the **task bundle** is the
user's `.vibesys/tasks/<name>/` directory, whose `OBJECTIVE.md` states the
goal and benchmark contract; a **golden** is a checked-in rendered prompt a
test compares byte for byte. [references/mechanics.md](references/mechanics.md)
explains how the pieces are assembled.

## Builds on

- `software-design`: one source of truth per fact (the reply model owns field
  names, the pack owns domain text); Python computes facts, templates word them.
- `testing`: goldens and paired positive and negative cases. The
  `write-vibesys-tests` skill covers this repository's test harness.

## When this applies

- Any prompt template (`.j2`) of a strategy, the shared prompts, the runtime
  library or the server's chat, or any domain pack role or modality file.
- A new domain role file, modality, backend fragment or environment note.
- The prompt renderer, the domain-section rendering and the vs-prompts library.
- Renaming, adding or removing a field of a strategy's reply model.
- Not product skills shipped to optimization agents; see
  [skill-metadata.md](../../../docs/contributing/skill-metadata.md).

## Steps

1. **Put each fact in the layer that owns it.** Every strategy and domain: the
   shared core-protocol or shared templates. One strategy: its own templates.
   One problem space: the pack's role file. One backend or environment: its
   fragment. One task: the bundle's `OBJECTIVE.md`. A strategy or shared
   template never tests the run's domain
   (`test_a_template_outside_a_domain_pack_does_not_test_the_domain`), and a
   pack's text reaches only its own runs
   (`test_every_role_prompt_carries_its_domains_section_and_no_other_domains_text`).
   Task rules kept in a pack are Unenforced: telling a task's contract from a
   domain's needs judgment; ask whether another task in the domain wants it.
2. **Write the words in the template; Python passes data.** No literals, `+`,
   f-strings, `.format`, `.join`, `.replace` or `dedent` on agent-bound text,
   and no edits to a render result
   (`test_prompts_in_migrated_modules_are_rendered_from_templates`). Only the
   renderer builds a `RenderedPrompt` (`test_only_the_renderer_mints_rendered_prompts`).
   The code that builds a run's domain sections passes run facts; the pack
   decides what to say about them.
3. **Pass every variable a template reads, and read every one you pass.** A
   missing one raises (`test_missing_var_raises_undefined_error`). A new
   variable joins the representative context that the packaged-render check
   builds (`test_every_packaged_prompt_renders_without_checkout_fallback`). One
   passed but deliberately unread carries
   `{# vs-prompts:unused: <name> - <reason> #}`; environment templates are held
   to it by `test_environment_templates_use_every_kwarg_their_call_site_passes`.
4. **Name reply fields only through `reply`:** `{{ reply.parent_hypothesis_id }}`,
   never the backticked name (`test_a_template_names_reply_fields_only_through_reply`).
   `reply` holds only fields the run offers, so naming another raises
   `vs_prompts.api.UnknownReplyFieldError`. A field's meaning goes in its
   Pydantic `description`, which the agent also receives.
   A reply field that makes a choice (a status, a verdict, a bar, a base) declares the
   prompt context fields it is decided from with `DecidedFrom` in its `Annotated` type,
   and the prompt must show them (`test_the_rendered_prompt_carries_every_declared_input`).
   Every reply of every registered search is found through its roles, so a new reply or
   decision field fails until it declares (`test_every_decision_field_of_a_reply_declares_its_inputs`);
   a measurement the agent only copies is listed with its reason, and a role whose reply declares
   inputs gets a case in the check's case table.
5. **Domain text goes in role files** that every strategy prints in one shared
   slot. All role files get the same variable set, so read what the
   domain-section renderer passes before using a variable. A modality is
   declared on the pack's definition, with one partial per role that the role
   file includes (`test_every_serving_modality_changes_the_implementer_section`).
6. **A backend fragment exists for every backend:** register the name with the
   fragment set and add one file per backend (empty means a deliberate skip).
   The backend renderer validates this when constructed
   (`test_every_backend_has_registered_fragment`,
   `test_every_backend_fragment_validates`). Read it as `{{ <name> }}`.
7. **Every strategy shows the environment notes**, through the notes variables
   or the shared run-context template
   (`test_a_strategy_prompt_directory_reads_the_environment_notes`).
8. **Name only paths and tools the agent has.** Cited paths must exist in its
   workspace or mounts (`test_every_packaged_prompt_renders_without_checkout_fallback`
   asserts every citation); only offered profiler tools may be named
   (`test_offer_withholds_exactly_the_driver_tools_without_an_editor_gpu`).
9. **Guard a domain's distinctive vocabulary** by adding its terms to the
   domain leak list (`test_domain_specific_keywords_do_not_leak_to_vetted_domains`).

## Testing

- Run the prompt architecture tests, the domain prompt tests and the
  vs-prompts tests with `uv run pytest`, then the goldens covering what you
  changed. CI fails if any of them breaks; `./support/repoctl/repoctl test`
  runs the groups a change selects.
- Regenerate a golden only for an intended change and read its diff as the
  prompt diff; [references/goldens.md](references/goldens.md) explains the
  workflow. Unenforced: only a reviewer can tell an intended change from a
  regression.
- Render in tests as production does, with the `reply` variables the role
  offers. Unenforced: no check compares a test's context with production's.
- Before a PR run `test_every_packaged_prompt_renders_without_checkout_fallback`:
  it renders every template from the built wheel, so it also catches a prompt
  file the package-data globs miss.

## Known failure modes

- Serving modality partials in shared prompts reached non-serving runs and
  missed strategies: `test_modality_text_never_reaches_a_generic_run`.
- The pack reached only some strategies' role prompts:
  `test_every_role_prompt_carries_its_domains_section_and_no_other_domains_text`.
- A few issue-queue templates still test the run's domain under counted
  exemptions. Move that text into the pack and lower the count; never add an
  entry (`test_every_exemption_still_matches_exactly`).
- Typed reply field names drifted from the schema:
  `test_a_template_names_reply_fields_only_through_reply`.
- A new template variable broke the packaged render until the representative
  context learned it: `test_every_packaged_prompt_renders_without_checkout_fallback`.
- A new strategy forgot the environment notes, so live Slurm agents never
  learned how commands run:
  `test_a_strategy_prompt_directory_reads_the_environment_notes`.
- Prompts named profiler tools that fail without an editor GPU:
  `test_offer_withholds_exactly_the_driver_tools_without_an_editor_gpu`.
- A profiler prompt printed an absent benchmark command:
  `test_an_absent_benchmark_command_points_at_the_objective`.
- CUDA and ROCm profiling fragments carry serving steps that every domain on
  that backend reads. Unenforced: telling backend mechanics from serving prose
  needs judgment; put new serving steps in the pack's profiler role file.
- Editing a golden fixture that no test reads and taking a green suite as
  coverage. Unenforced: no check maps fixtures to their readers; confirm a
  test reads the fixture before editing it.

## Sources of truth

- The vs-prompts library README: strict rendering, contracts, reply fields.
- The prompt renderer's template roots and backend fragment set.
- The domain registry and the domain role set, reached through the product
  facade. [domains.md](../../../docs/contributing/domains.md): pack anatomy and
  role file variables.
- [references/mechanics.md](references/mechanics.md): how a prompt is assembled.
