"""Plugin registrations for the single-agent search presets: the core path.

Every entrypoint resolves `single-agent` and `profile-guided-single-agent` to these
registrations. The run is a vs-core step function driven by the vs-runtime shell;
`single_core` composes it.
"""

from vibesys.orchestration.single.models import ProfileGuidedSingleOptions, SingleOptions
from vibesys.single_core import single_core_registration

REGISTRATION = single_core_registration("single-agent", SingleOptions)
PLUGIN = REGISTRATION.plugin
PROFILE_GUIDED_REGISTRATION = single_core_registration(
    "profile-guided-single-agent", ProfileGuidedSingleOptions
)
PROFILE_GUIDED_PLUGIN = PROFILE_GUIDED_REGISTRATION.plugin

__all__ = ["PLUGIN", "PROFILE_GUIDED_PLUGIN", "PROFILE_GUIDED_REGISTRATION", "REGISTRATION"]
