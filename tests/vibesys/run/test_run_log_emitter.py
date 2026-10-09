"""The run log keeps reporting on the event stream after the run's file is closed."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vibesys.run.integration import run_log_emitter
from vs_agent.api import NullAgentEventSink
from vs_project.api import RunLogger


class _RecordingEvents(NullAgentEventSink):
    """Collects diagnostic output the way a run's event sink would."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def agent_output(self, content: str, **_options: object) -> None:
        self.chunks.append(content)


@given(st.lists(st.text(alphabet=st.characters(codec="ascii", exclude_characters="\n\r"))))
def test_a_message_after_the_run_log_closed_still_reaches_the_event_stream(
    messages: list[str],
) -> None:
    events = _RecordingEvents()
    with tempfile.TemporaryDirectory() as directory:
        # No stderr tee: the logger must not touch the process-wide stderr in a test.
        logger = RunLogger(Path(directory), tee_stderr=False, emit=run_log_emitter(events))
        logger.close()
        for message in messages:
            logger.lprint(message)

    assert events.chunks == [message + "\n" for message in messages]
