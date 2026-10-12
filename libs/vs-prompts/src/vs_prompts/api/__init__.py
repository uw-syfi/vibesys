"""Public Jinja2 rendering and template-contract helpers.

Two correctness properties every consumer gets for free:

- **Under-supply** is caught at render time: :class:`~vs_prompts.api.TemplateRenderer`
  always uses ``jinja2.StrictUndefined``, so a template referencing a
  variable no caller supplied raises immediately instead of silently
  rendering empty.
- **Over-supply** (a variable is supplied but a template never references
  it, so its content silently never reaches the output) is checked
  separately via :class:`~vs_prompts.api.TemplateContract`, since no
  undefined-variable check can see that direction.

:class:`~vs_prompts.api.FragmentFamily` generalizes "every variant of
this discriminator must define every required small fragment, an empty file
is a deliberate skip" to any per-key fragment set.

:class:`~vs_prompts.api.ReplyFields` is the ``reply`` template variable for a turn with a
reply schema: ``{{ reply.some_field }}`` renders the backticked field name and fails
rendering when the schema does not offer the field.

:class:`~vs_prompts.api.RenderedPrompt` is the typed result of a render. Only
the renderer constructs one, so agent-bound text can be told apart from text
assembled in Python.
"""

from vs_prompts.contract import (
    ContractViolation,
    TemplateContract,
    UnresolvedInclude,
    filter_skip_marked,
    resolve_free_variables,
)
from vs_prompts.fragments import FragmentFamily
from vs_prompts.renderer import RenderedPrompt, TemplateRenderer
from vs_prompts.reply_fields import (
    ReplyField,
    ReplyFields,
    UnknownReplyFieldError,
    reply_fields,
)

__all__ = [
    "ContractViolation",
    "FragmentFamily",
    "RenderedPrompt",
    "ReplyField",
    "ReplyFields",
    "TemplateContract",
    "TemplateRenderer",
    "UnknownReplyFieldError",
    "UnresolvedInclude",
    "filter_skip_marked",
    "reply_fields",
    "resolve_free_variables",
]
