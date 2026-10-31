"""Working copies of a state's values: an event reads through them and pays only for what it changes."""

from __future__ import annotations

from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from protobuf import Message
    from rpds import HashTrieMap


class MapDraft[K, V]:
    """A persistent map an event reads and writes; the successor map shares what it left alone."""

    def __init__(self, base: HashTrieMap[K, V]) -> None:
        """Start from ``base``, which is never changed."""
        self._base = base
        self._written: dict[K, V] = {}

    @overload
    def get(self, key: K) -> V | None: ...

    @overload
    def get[D](self, key: K, default: D) -> V | D: ...

    def get(self, key: K, default: object = None) -> object:
        """The value at ``key``, or ``default``."""
        if key in self._written:
            return self._written[key]
        return self._base.get(key, default)

    def __getitem__(self, key: K) -> V:
        return self._written[key] if key in self._written else self._base[key]

    def __contains__(self, key: K) -> bool:
        return key in self._written or key in self._base

    def __setitem__(self, key: K, value: V) -> None:
        self._written[key] = value

    def commit(self) -> HashTrieMap[K, V]:
        """The map after the event: ``base`` with the writes, sharing the rest of it."""
        merged = self._base
        for key, value in self._written.items():
            merged = merged.insert(key, value)
        return merged


class MessageDraft[M: Message]:
    """A message kept as its wire form until read, and encoded again only if it was replaced."""

    def __init__(self, kind: type[M], wire: bytes | None) -> None:
        """Hold ``wire``, the encoding of a ``kind`` message or None."""
        self._kind = kind
        self._wire = wire
        self._value: M | None = None
        self._read = False
        self._replaced = False

    @property
    def value(self) -> M | None:
        """The message, decoded on first read."""
        if not self._read:
            self._read = True
            self._value = None if self._wire is None else self._kind.from_binary(self._wire)
        return self._value

    @value.setter
    def value(self, value: M | None) -> None:
        self._read = True
        self._replaced = True
        self._value = value

    @property
    def wire(self) -> bytes | None:
        """The message's encoding: the wire form held, unless the message was replaced."""
        if not self._replaced:
            return self._wire
        return None if self._value is None else self._value.to_binary()
