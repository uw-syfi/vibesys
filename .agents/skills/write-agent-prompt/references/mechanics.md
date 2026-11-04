# Prompt mechanics

How a role prompt is assembled, for when a step in the skill needs more than
its one-line rule. Read it before adding a domain role file, a modality, a
backend fragment or a template root, or when a render fails in a way the
error does not explain.

## Rendering

Every agent-facing text is a Jinja template rendered by the vs-prompts
`TemplateRenderer`:

- Strict undefined is the only mode: a variable no caller passed raises.
  `{% if x is defined %}` still works.
- A renderer has a root and fallback roots, searched in order. For a strategy
  the order is the strategy's own templates, then the shared templates, then
  the prompts root, then the core protocol. A strategy never resolves a
  template from a sibling strategy.
- The renderer and the template contract also check the other direction: a
  variable passed but never read. Both honor the
  `{# vs-prompts:unused: <name> - <reason> #}` marker.
- `RenderedPrompt` is minted only by the renderer; concatenating it yields a
  plain `str`, which the prompt-template architecture check reports at any
  prompt sink (an agent turn's message, a system prompt argument, a parameter
  typed `RenderedPrompt`, a tool server's return value).

## Role system prompts

A strategy's fixed system prompt per role is a template too, rendered once
when the role is declared, before any run exists, so it has no `reply`
variable.

## Reply fields

A turn's reply model is a Pydantic model the strategy registers in its
search's reply schemas. At render time the runtime turns the JSON Schema the
agent receives (after the run's capabilities removed fields) into `reply`, a
`vs_prompts.api.ReplyFields`. `{{ reply.x }}` renders `` `x` ``, and
`{{ reply.x.name }}` the bare name. A context that describes other turns'
replies gets each under its own variable, such as `implementer_reply`.

## Domain packs

- The run's wiring resolves its domain, checks its modality, and renders every
  role file with one uniform variable set. The result is the plan's
  `domain_sections` variable: one rendered text per domain role, empty for a
  role the pack does not address.
- Strategy templates print it through the shared `domain_section` macro; an
  empty section adds no bytes.
- A role file renders from its pack's own directory, so it may include the
  pack's partials. With no single-agent file, the single-agent section is the
  implementer and judge sections joined.
- A modality (for example text generation or speech to text) is declared on
  the pack's definition with a default; its partial is included by the role
  file. A run naming a modality the pack does not declare fails with
  `modality_not_in_domain`.
- A new pack is registered in the domain registry; its byte digests join the
  domain prompt snapshot manifest.

## Backend fragments

- The backend prompt renderer injects every fragment of the run's backend as a
  variable named by the file stem, on every render; an explicit keyword
  overrides it.
- A new backend needs its name in the fragment set and a fragment class
  registered for it, next to the compute backend enum in vs-sandbox.
- An empty fragment file is a hard skip; short prose saying the topic is not
  supported yet is a soft skip that tells the agent the topic exists.

## Run-environment notes

Each run environment kind has prompt notes and, for remote environments,
runtime notes. The run passes the rendered notes as the run facts'
environment notes; strategies print them through the shared run-context
template or a runtime-notes variable.
