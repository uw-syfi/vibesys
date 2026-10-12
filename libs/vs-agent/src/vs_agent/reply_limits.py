"""The size and shape a model's reply may have before the turn counts as failed.

A model that degenerates can keep emitting whitespace (or any filler) inside an
open JSON value for minutes, and the provider CLI then retries the stream
instead of ending the turn. The orchestrator would wait on a reply that cannot
parse. These limits end such a turn as soon as the offending text arrives, as an
invalid response, which every caller already answers with one bounded
correction turn.

The limits are VibeSys policy (how much reply the system is willing to read), not
provider knowledge, so they live here and not in agentshim. They are far above
any legitimate reply: a typed reply is a JSON object of a few kilobytes, and a
free-text reply is a report, not a document.
"""

from __future__ import annotations

import re
from enum import StrEnum

MAX_REPLY_BYTES = 1 << 20
"""The most text one turn's reply may hold, counted in UTF-8 bytes."""

MAX_WHITESPACE_RUN = 1 << 14
"""The longest unbroken run of whitespace characters a reply may hold."""

_TOKEN = re.compile(r"\s+|\S+")


class ReplyBreach(StrEnum):
    """Why a reply was refused; the name is shown to the agent that must correct it."""

    OVERSIZED = "oversized_reply"
    DEGENERATE = "degenerate_reply"


class ReplyGuard:
    """Watch the text of one turn as it arrives and name the first limit it crosses.

    Feed every text chunk of the turn in order; chunk boundaries do not matter,
    including inside a whitespace run. After a breach the guard stays breached.
    """

    def __init__(self) -> None:
        """Start a turn with no text seen."""
        self._bytes = 0
        self._run = 0
        self._breach: str | None = None

    def feed(self, text: str) -> str | None:
        """Account for *text* and return the breach detail once a limit is crossed, else ``None``."""
        if self._breach is not None:
            return self._breach
        self._bytes += len(text.encode())
        if self._bytes > MAX_REPLY_BYTES:
            self._breach = (
                f"{ReplyBreach.OVERSIZED}: the reply passed {MAX_REPLY_BYTES} bytes. "
                "Reply with only what was asked for, without padding or repetition."
            )
            return self._breach
        for token in _TOKEN.findall(text):
            if token.isspace():
                self._run += len(token)
                if self._run > MAX_WHITESPACE_RUN:
                    self._breach = (
                        f"{ReplyBreach.DEGENERATE}: the reply held more than "
                        f"{MAX_WHITESPACE_RUN} whitespace characters in a row. "
                        "Reply with only the compact value the schema asks for, "
                        "without padding."
                    )
                    return self._breach
            else:
                self._run = 0
        return None


def reply_breach(text: str) -> str | None:
    """The breach detail of a complete reply, or ``None`` when it is within the limits."""
    return ReplyGuard().feed(text)
