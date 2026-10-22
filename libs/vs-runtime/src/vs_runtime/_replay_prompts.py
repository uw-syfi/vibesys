"""The renderer boundary for keyed operations accepts only classified protobuf contexts."""

from dataclasses import InitVar, dataclass
from typing import TYPE_CHECKING

from protobuf import Message

from vs_core.api.replay import ClassifiedContext, ReplayCall, SemanticKey, classify_context
from vs_prompts.api import RenderedPrompt, TemplateRenderer
from vs_proto.api import replay

if TYPE_CHECKING:
    from collections.abc import Mapping


_PROMPT_TOKEN = object()


@dataclass(frozen=True)
class ReplayPrompt:
    """The exact rendered policy and the classified data from which it was rendered."""

    template: str
    context: ClassifiedContext
    text: RenderedPrompt
    token: InitVar[object]

    def __post_init__(self, token: object) -> None:
        if token is not _PROMPT_TOKEN:
            problem = "ReplayPrompt is produced only by ReplayPromptRenderer"
            raise TypeError(problem)

    def call[T: Message](
        self, key: SemanticKey, reply: type[T], *, role: str, contract: str
    ) -> ReplayCall[T]:
        """Construct a typed request without allowing prompt variables to bypass classification."""
        # Reclassify under the operation's semantic contract, including current causality
        # when the scheduler registers the call. The renderer's own contract is provenance.
        classified = classify_context(self.context.message(), contract=contract)
        return ReplayCall(
            key,
            classified,
            contract,
            replay.ReplayPolicy(
                role=role,
                template=self.template,
                content=self.text.encode(),
                reply_type=reply.desc().type_name,
            ),
            reply,
        )


class ReplayPromptRenderer:
    """No arbitrary kwargs, implicit run globals or post-render text edits enter replay prompts.

    Every variable the template can observe comes from a validated protobuf
    context with identity/policy field options. Existing generic rendering stays
    available to the four strategies that have not crossed this boundary yet.
    """

    def __init__(self, renderer: TemplateRenderer) -> None:
        self._renderer = renderer

    def render(self, template: str, context: Message, *, contract: str) -> ReplayPrompt:
        classified = classify_context(context, contract=contract)
        message = classified.message()
        variables: Mapping[str, object] = {
            field.name: getattr(message, field.name) for field in message.desc().fields
        }
        text = self._renderer.render_template(template, **variables)
        return ReplayPrompt(template, classified, text, _PROMPT_TOKEN)
