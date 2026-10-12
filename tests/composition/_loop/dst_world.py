"""A conformance `Subject` and one of its scripted worlds, as a vs-dst `World`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_dst.api import RunSetup

if TYPE_CHECKING:
    from tests.composition._loop.conformance import Subject, World

    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer


class SubjectWorld:
    """The strategy a `Subject` describes, run against one of its scripted worlds.

    `setup` is the subject's immutable description, which vs-dst shares between copies; the
    script is the part whose position a copy carries.
    """

    def __init__(self, subject: Subject, script: World) -> None:
        """Run ``subject``'s strategy against ``script``."""
        self.setup = RunSetup(subject.new_strategy, subject.harness, subject.schemas)
        self.script = script

    def __call__(self, request: Request, core: CoreState) -> Answer:
        """What the script answers for ``request``."""
        return self.script(request, core)
