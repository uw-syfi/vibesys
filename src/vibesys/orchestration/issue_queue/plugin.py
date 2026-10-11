"""Plugin registration for the issue queue: the core path.

Every entrypoint resolves `plain` to this registration. The run is a vs-core step function
driven by the vs-runtime shell; `issue_queue_core` composes it.
"""

from vibesys.issue_queue_core import issue_queue_core_registration

REGISTRATION = issue_queue_core_registration()
PLUGIN = REGISTRATION.plugin

__all__ = ["PLUGIN", "REGISTRATION"]
