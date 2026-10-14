# Prompt mechanics

How a role prompt is assembled, for when a step in the skill needs more than
its one-line rule. Read it before adding a domain role file, a modality, a
backend fragment or a template root, or when a render fails in a way the
error does not explain.

## Rendering

Every agent-facing text is a Jinja template rendered by
`libs/vs-prompts/src/vs_prompts/renderer.py:TemplateRenderer`:

- `StrictUndefined` is the only mode: a variable no caller passed raises
  `jinja2.UndefinedError`. `{% if x is defined %}` still works.
- A renderer has a root and fallback roots, searched in order. For a strategy,
  `src/vibesys/prompts/renderer.py:_build_env` searches the strategy's own
  prompts directory, then `src/vibesys/prompts/shared/`, then
  `src/vibesys/prompts/`, then `src/vibesys/prompts/core_protocol/`. A strategy
  never resolves a template from a sibling strategy's directory.
- `TemplateRenderer.unused_kwargs` and
  `libs/vs-prompts/src/vs_prompts/contract.py:TemplateContract` check the other
  direction: a variable passed but never read. Both honor the
  `{# vs-prompts:unused: <name> - <reason> #}` marker.
- `RenderedPrompt` is minted only by the renderer; concatenating it yields a
  plain `str`, which `tests/architecture/test_prompt_templates.py` reports at
  any prompt sink (an agent turn's message, a `system_prompt=` argument, a
  parameter typed `RenderedPrompt`, a tool server's return value).

## Role system prompts

A strategy's fixed system prompt per role is a template too, rendered once
when the role is declared, before any run exists, so it has no `reply`
variable. Example: `src/vibesys/multi_roles.py:_role` renders
`<role>_system.j2` from the multi strategy's prompts directory.

## Reply fields

A turn's reply model is a Pydantic model the strategy registers in its
search's `reply_schemas`. At render time
`libs/vs-runtime/src/vs_runtime/_render_operation.py:reply_variable` turns
the JSON Schema the agent receives (after the run's capabilities removed
fields) into `reply`, a `vs_prompts.api.ReplyFields`. `{{ reply.x }}` renders
`` `x` ``, and `{{ reply.x.name }}` the bare name. A context that describes
other turns' replies gets each under its own variable, such as
`implementer_reply`.

## Domain packs

- `src/vibesys/core_wiring.py:domain_prompt_sections` resolves the run's
  domain, checks its modality, and calls
  `src/vibesys/domains/rendering.py:render_domain_sections` with the uniform
  variable set. The result is the plan's `domain_sections` variable: one
  rendered text per `src/vibesys/domains/base.py:DomainRole`, empty for a role
  the pack does not address.
- Strategy templates print it with
  `{{ domain_section(domain_sections, '<role>') }}` from
  `src/vibesys/prompts/core_protocol/domain_section.j2`; an empty section adds
  no bytes.
- A role file renders from its pack's own directory, so it may include the
  pack's partials. With no `single_agent.md`, the single-agent section is the
  implementer and judge sections joined
  (`src/vibesys/prompts/shared/_domain/single_agent_fallback.j2`).
- A modality (`text_generation`, `speech_to_text`, ...) is declared on the
  pack's `DomainDefinition` (`modalities`, `default_modality`); its partial is
  `prompts/modality/<modality>/<role>.md`, and the role file includes it with
  `{% include "modality/" ~ modality ~ "/<role>.md" %}`. A run naming a
  modality the pack does not declare fails with `modality_not_in_domain`.
- A new pack is registered in `src/vibesys/domains/registry.py:DOMAINS`;
  its byte digests join
  `tests/vibesys/orchestration/domains/fixtures/prompt_sha256.json`.

## Backend fragments

- `src/vibesys/prompts/renderer.py:BackendPromptRenderer` injects every
  fragment of the run's backend as a variable named by the file stem, on every
  render; an explicit keyword overrides it.
- The names are `src/vibesys/prompts/renderer.py:ComputeBackendFragment.NAMES`.
  A new backend also needs a `ComputeBackendFragment` subclass registered in
  `_FRAGMENT_IMPLS` in the same module; the backend enum is
  `libs/vs-sandbox/src/vs_sandbox/compute_backends.py:ComputeBackend`.
- An empty fragment file is a hard skip; short prose saying the topic is not
  supported yet is a soft skip that tells the agent the topic exists.

## Run-environment notes

`src/vibesys/prompts/environments/<kind>/` holds `prompt_notes.j2` and, for
remote environments, `runtime_notes.j2`. The run passes the rendered notes as
`RunFacts.environment_notes`; strategies print them through
`src/vibesys/prompts/core_protocol/run_context.j2` or a `runtime_notes`
variable.

## Stale documentation

- `src/vibesys/prompts/backend/README.md` names a `Prompt` class (it is
  `BackendPromptRenderer`), puts `ComputeBackend` in `vibesys/constants.py`
  (it is re-exported there from `vs_sandbox`), and registers backends in
  `vibesys/backends/__init__.py` (they register in
  `libs/vs-sandbox/src/vs_sandbox/compute_backends.py:_register_defaults`).
- The module docstring of `src/vibesys/prompts/renderer.py` cites
  `loops/multi/implementer_prompt.j2`, a directory that no longer exists.
- [domains.md](../../../../docs/contributing/domains.md) describes a `hooks.py`
  with an `EnvironmentHooks` implementation attached to the definition; no
  such type or field exists.
- `tests/vibesys/golden/helpers.py` says to regenerate with `UPDATE_GOLDEN`;
  only its `assert_exact_text` is still called, and it reads
  `UPDATE_PROMPT_SNAPSHOTS`.
