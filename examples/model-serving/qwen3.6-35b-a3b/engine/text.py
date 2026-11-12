"""Turn generated token ids into streamed text, one delta per token.

``Detokenizer`` emits the text each new token completes, holding back a token
that ends inside a multi-byte character until the character is whole.
``StopMatcher`` withholds just enough text to recognize a stop string that
spans deltas, and cuts the output before the first match.
"""

from __future__ import annotations

from typing import Protocol

_REPLACEMENT = "�"


class Decoder(Protocol):
    def decode(self, ids: list[int], skip_special_tokens: bool = ...) -> str: ...


class Detokenizer:
    """Incremental decoding over a short window of recent ids."""

    def __init__(self, tokenizer: Decoder) -> None:
        self._tokenizer = tokenizer
        self._ids: list[int] = []
        self._prefix = 0
        self._read = 0

    def _decode(self, ids: list[int]) -> str:
        return self._tokenizer.decode(ids, skip_special_tokens=True)

    def push(self, token_id: int) -> str:
        """Add one id; return the text it completes (possibly empty)."""
        self._ids.append(token_id)
        before = self._decode(self._ids[self._prefix : self._read])
        after = self._decode(self._ids[self._prefix :])
        if len(after) > len(before) and not after.endswith(_REPLACEMENT):
            self._prefix, self._read = self._read, len(self._ids)
            return after[len(before) :]
        return ""

    def flush(self) -> str:
        """Text still pending at the end of generation."""
        before = self._decode(self._ids[self._prefix : self._read])
        after = self._decode(self._ids[self._prefix :])
        self._prefix = self._read = len(self._ids)
        return after[len(before) :]


class StopMatcher:
    """Releases text that cannot be the start of a stop string; stops at the first match."""

    def __init__(self, stops: tuple[str, ...]) -> None:
        self._stops = stops
        self._hold = max((len(s) for s in stops), default=1) - 1
        self._pending = ""
        self.stopped = False

    def push(self, text: str) -> str:
        """Feed new text; return what may be emitted now."""
        if self.stopped:
            return ""
        self._pending += text
        cut = min((i for s in self._stops if (i := self._pending.find(s)) >= 0), default=-1)
        if cut >= 0:
            self.stopped = True
            out, self._pending = self._pending[:cut], ""
            return out
        if self._hold == 0:
            out, self._pending = self._pending, ""
            return out
        out = self._pending[: max(len(self._pending) - self._hold, 0)]
        self._pending = self._pending[len(out) :]
        return out

    def flush(self) -> str:
        """Release withheld text when generation ends without a match."""
        out, self._pending = ("" if self.stopped else self._pending), ""
        return out
