"""A one-entry memo of the last strictly decoded document, keyed on its exact bytes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class DecodeMemo[DocumentT]:
    """Decode a document once per distinct bytes, so an unchanged document costs no decode.

    Strict decoding (including the duplicate-key scan) costs the size of the document, and
    a store reads the document on every operation, so a run's cost grew with commits times
    document size. The memo holds the document last decoded or last written. Bytes that
    equal the remembered bytes are exactly what decoding them again would give, since decoding
    is a pure function of the bytes; any other bytes are decoded, and refused if damaged,
    exactly as without the memo. Documents are frozen models, so sharing one is safe.
    """

    def __init__(
        self, decode: Callable[[bytes], DocumentT], on_decode: Callable[[], None] | None = None
    ) -> None:
        """Wrap the strict ``decode``; ``on_decode`` observes each decode that runs."""
        self._decode = decode
        self._on_decode = on_decode
        self._remembered: tuple[bytes, DocumentT] | None = None

    def decode(self, source: bytes) -> DocumentT:
        """The document ``source`` decodes to, decoding only when it is new bytes."""
        if self._remembered is not None and self._remembered[0] == source:
            return self._remembered[1]
        if self._on_decode is not None:
            self._on_decode()
        document = self._decode(source)
        self._remembered = (source, document)
        return document

    def remember(self, source: bytes, document: DocumentT) -> None:
        """Record that ``source`` is the encoding of ``document``, which the caller built."""
        self._remembered = (source, document)
