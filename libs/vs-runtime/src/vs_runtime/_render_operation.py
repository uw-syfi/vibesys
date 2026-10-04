"""Owner of the prompt-rendering operation: templates in, stored artifacts out.

Python passes data only. The request carries a typed context whose ``template``
names one template file, and its other fields are the template's variables. The
template owns all wording, and ``vs_prompts`` renders it with strict undefined
variables, so a missing variable is a typed failure and never an empty string.
Artifacts are content addressed, so the operation is idempotent by construction.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol, cast

from jinja2.exceptions import TemplateError

from vs_core.api import ArtifactId, ArtifactRef
from vs_runtime._operation_catalog import Applied, Inspection, NotApplied

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import BaseModel

    from vs_core.api import OperationRequest
    from vs_prompts.api import TemplateRenderer
    from vs_runtime._artifact_store import ArtifactStore
    from vs_runtime._core_requests import ExecutionContext


class RenderRequest(Protocol):
    """The request shape this owner serves, declared by the strategy that issues it."""

    subject: str
    ordinal: int
    context: BaseModel


class RenderArtifactsOwner:
    """Render one role prompt and store it, idempotent per (subject, ordinal)."""

    def __init__(self, renderer: TemplateRenderer, store: ArtifactStore) -> None:
        """Bind the template root and the artifact store."""
        self._renderer = renderer
        self._store = store

    def _render(self, request: OperationRequest) -> tuple[bytes, ArtifactId] | None:
        render = cast("RenderRequest", request)
        context = render.context
        template = str(getattr(context, "template", ""))
        variables = {name: getattr(context, name) for name in type(context).model_fields}
        variables.pop("template", None)
        try:
            text = self._renderer.render_template(f"{template}.j2", **variables)
        except TemplateError:
            return None
        return text.encode(), ArtifactId(root=f"render:{render.subject}:{render.ordinal}")

    def _outcome(self, content: bytes, artifact_id: ArtifactId) -> Mapping[str, object]:
        digest = hashlib.sha256(content).hexdigest()
        return {
            "status": "succeeded",
            "prompts": (ArtifactRef(artifact_id=artifact_id, digest=digest),),
        }

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> Mapping[str, object]:
        """Render, store, and name the artifact by its content digest."""
        del context
        rendered = self._render(request)
        if rendered is None:
            return {"status": "failed"}
        content, artifact_id = rendered
        self._store.write(content)
        return self._outcome(content, artifact_id)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        """Applied when the rendered bytes are already stored, else provably not applied."""
        del context
        rendered = self._render(request)
        if rendered is None:
            return Applied({"status": "failed"})
        content, artifact_id = rendered
        if self._store.contains(content):
            return Applied(self._outcome(content, artifact_id))
        return NotApplied()
