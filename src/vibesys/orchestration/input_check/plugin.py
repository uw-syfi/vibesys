"""Plugin registration for the gates-only input check."""

from vibesys.input_check_core import input_check_core_registration

REGISTRATION = input_check_core_registration()
PLUGIN = REGISTRATION.plugin

__all__ = ["PLUGIN", "REGISTRATION"]
