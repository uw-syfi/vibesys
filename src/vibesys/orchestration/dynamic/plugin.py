"""Plugin registration for dynamic portfolio hypothesis search: the core path.

Every entrypoint resolves `dynamic` to this registration. The run is a vs-core
step function driven by the vs-runtime shell; `dynamic_core` composes it.
"""

from vibesys.dynamic_core import dynamic_core_registration

REGISTRATION = dynamic_core_registration()
PLUGIN = REGISTRATION.plugin

__all__ = ["PLUGIN", "REGISTRATION"]
