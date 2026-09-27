"""Installed helper programs used inside remote evaluation sandboxes."""

from pathlib import Path

from vs_sandbox import modal_evaluator, skypilot_evaluator
from vs_sandbox.modal_evaluator import encode_setup_command

MODAL_EVALUATOR_HELPER = Path(modal_evaluator.__file__).resolve()
SKYPILOT_EVALUATOR_HELPER = Path(skypilot_evaluator.__file__).resolve()

__all__ = [
    "MODAL_EVALUATOR_HELPER",
    "SKYPILOT_EVALUATOR_HELPER",
    "encode_setup_command",
]
