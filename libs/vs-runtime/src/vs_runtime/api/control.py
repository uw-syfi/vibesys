"""Control admission contracts without run execution or infrastructure factories."""

from vs_runtime._operator_control import CONTROL_REQUEST, ControlAction
from vs_runtime._run_end import recorded_ending, recorded_epoch

__all__ = ["CONTROL_REQUEST", "ControlAction", "recorded_ending", "recorded_epoch"]
