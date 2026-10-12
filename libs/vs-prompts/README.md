# vs-prompts

## Responsibility

This package renders Jinja2 templates strictly and checks that required context
and fragment variants appear in templates. Applications own template content,
directory layout, and the context they pass to the renderer.

This is an internal import package shipped by the `vibesys` distribution. It
is not published as a separate Python distribution.

## Concepts

The public API provides four related checks:

- `TemplateRenderer` renders `.j2` files and template strings with
  `jinja2.StrictUndefined`: a template referencing a variable no caller
  supplied raises `UndefinedError` at render time instead of silently
  producing an empty string. `{% if x is defined %}` guards keep working,
  since Jinja's `defined` test never evaluates the underlying value.
- `TemplateContract` catches the opposite direction: a variable a caller
  *does* supply but a template silently never references, so its content
  never reaches the rendered output. No undefined-variable check can see
  this direction; `TemplateContract` statically resolves each template's
  free variables (recursing through static `{% include %}`s, which share
  the parent scope) and flags any required variable a template omits
  without an explicit `{# vs-prompts:unused: <var> #}` skip marker.
- `FragmentFamily` generalizes "every variant of a discriminator must define
  every required small fragment, an empty file is a deliberate skip" to any
  per-key fragment set: the same contract `ComputeBackend` fragments need,
  without hardcoding that enum.
- `reply_fields` turns the JSON Schema an agent receives for its reply into the
  `reply` template variable. `{{ reply.parent_hypothesis_id }}` renders the
  backticked field name (`{{ reply.perf_metric.name }}` the bare name), and a
  name the schema does not offer raises `UnknownReplyFieldError`. The reply
  schema, trimmed by the run's capabilities, is the one source of truth for
  field names; templates keep the judgment about how to fill them.

## Usage

Bind a renderer to an application-owned template directory:

```python
from pathlib import Path

from vs_prompts.api import TemplateRenderer

renderer = TemplateRenderer(Path("prompts"))
prompt = renderer.render_template("agent.j2", objective="Reduce latency")
```

A missing template variable raises at render time. Use `TemplateContract` when
required application context must also be referenced by the template.
