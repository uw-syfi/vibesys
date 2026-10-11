"""Plugin registrations for the multi-agent search presets: the core path.

Every entrypoint resolves `multi-agent` and `profile-guided-multi-agent` to these
registrations. The run is a vs-core step function driven by the vs-runtime shell;
`multi_core` composes it.
"""

from vibesys.multi_core import multi_core_registration
from vibesys.orchestration.multi.models import MultiOptions, ProfileGuidedMultiOptions

REGISTRATION = multi_core_registration("multi-agent", MultiOptions)
PLUGIN = REGISTRATION.plugin
PROFILE_GUIDED_REGISTRATION = multi_core_registration(
    "profile-guided-multi-agent", ProfileGuidedMultiOptions
)
PROFILE_GUIDED_PLUGIN = PROFILE_GUIDED_REGISTRATION.plugin

__all__ = ["PLUGIN", "PROFILE_GUIDED_PLUGIN", "PROFILE_GUIDED_REGISTRATION", "REGISTRATION"]
