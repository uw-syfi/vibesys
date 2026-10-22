"""Persistent causal chains: one digest per request, no wrapper-dependent identity.

Merging a child's result or a channel delivery incorporates observed outcomes,
not the shape of the Python helpers carrying them. Shared prefixes and repeated
observations are retained once. Binary ancestry links and occurrence indexing
avoid rescanning a long fold for every delivery.
"""

from __future__ import annotations

from dataclasses import dataclass

from ._replay_identity import EMPTY_CAUSALITY, chain_dependency


@dataclass(frozen=True, eq=False)
class _Frontier:
    digest: bytes
    length: int = 0
    token: bytes = b""
    ancestors: tuple[_Frontier, ...] = ()


EMPTY_FRONTIER = _Frontier(EMPTY_CAUSALITY)


class CausalGraph:
    """Epoch-local persistent prefix sharing, never a persisted dependency list."""

    def __init__(self) -> None:
        self._nodes: dict[tuple[_Frontier, bytes], _Frontier] = {}
        self._occurrences: dict[bytes, list[_Frontier]] = {}
        self._coverage: dict[_Frontier, list[_Frontier]] = {}

    def append(self, frontier: _Frontier, token: bytes) -> _Frontier:
        known = self._nodes.get((frontier, token))
        if known is not None:
            return known
        ancestors = [frontier]
        while len(ancestors[-1].ancestors) >= len(ancestors):
            ancestors.append(ancestors[-1].ancestors[len(ancestors) - 1])
        node = _Frontier(
            chain_dependency(frontier.digest, token), frontier.length + 1, token, tuple(ancestors)
        )
        self._nodes[frontier, token] = node
        self._occurrences.setdefault(token, []).append(node)
        return node

    @staticmethod
    def _ancestor(ancestor: _Frontier, descendant: _Frontier) -> bool:
        distance = descendant.length - ancestor.length
        if distance < 0:
            return False
        while distance:
            jump = distance.bit_length() - 1
            descendant = descendant.ancestors[jump]
            distance = descendant.length - ancestor.length
        return descendant is ancestor

    def _contains(self, frontier: _Frontier, token: bytes) -> bool:
        return any(self._ancestor(node, frontier) for node in self._occurrences.get(token, ()))

    def _covered(self, current: _Frontier, incoming: _Frontier) -> bool:
        return self._ancestor(incoming, current) or any(
            self._ancestor(merged, current) for merged in self._coverage.get(incoming, ())
        )

    def merge(self, current: _Frontier, incoming: _Frontier) -> _Frontier:
        """Append unseen observations; a seen final token alone cannot cover a prefix."""
        if self._ancestor(current, incoming):
            return incoming
        if self._covered(current, incoming):
            return current
        original = incoming
        suffix = []
        while incoming.length and not self._covered(current, incoming):
            suffix.append(incoming.token)
            incoming = incoming.ancestors[0]
        for token in reversed(suffix):
            if not self._contains(current, token):
                current = self.append(current, token)
        self._coverage.setdefault(original, []).append(current)
        return current
