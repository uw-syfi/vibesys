"""Public API of the ``vs_loop_state`` library.

``RoundRecord`` and ``RoundHistory`` are the deliberate surface consumers
depend on. Everything else (JSON encoding, atomic writes, per-field legacy
migrations) is an implementation detail of ``vs_loop_state.agent`` and
``vs_loop_state.core``.
"""

from vs_loop_state.agent import RoundHistory, RoundRecord

__all__ = [
    "RoundHistory",
    "RoundRecord",
]
