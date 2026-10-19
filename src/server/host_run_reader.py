"""The host helper's ``run.reader``: the server a client reads and chats with a run through.

A run's chat is held by the run's server. A run with no live server (it ended, or its server was
stopped) is served read-only: the helper starts the run server in its read-only mode for that run
on demand, shares it among the clients that ask while it lives, and lets it end by itself once no
client holds a connection to it. The reply names the server's registry id, which the client opens
with ``run.connect`` as it does a live run's. A server the helper started this way is read-only: it
refuses every command, so a steer cannot reach a finished run (``run_read_only``).

Light: Pydantic, the standard library and ``server.host_envelope``. ``server.host_api`` imports
these messages into the helper's request and reply unions and gates them behind
``HelperFeature.CHAT_ENDED_RUNS``.

Public surface: ``RunReaderRequest`` and ``RunReaderReply``.
"""

from __future__ import annotations

from typing import Literal

from server.host_envelope import HelperReplyBase, HelperRequestBase, InstanceId, RunId


class RunReaderRequest(HelperRequestBase):
    """The server to read and chat with run ``run_id`` of ``project`` through."""

    type: Literal["run.reader"] = "run.reader"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""
    run_id: RunId


class RunReaderReply(HelperReplyBase):
    """The server for ``run.connect``: the run's live one, else a read-only one."""

    type: Literal["run.reader"] = "run.reader"
    instance_id: InstanceId
    read_only: bool
    """The server serves a finished run: it answers reads and refuses every command."""
