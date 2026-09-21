"""Single seam for reading agentshim's provider facts.

agentshim owns what a provider *is*: its binary, state directories, auth
environment variables, and container install recipe. VibeSys owns policy on
top of that: which providers it offers, which files inside a state directory
carry credentials, and what else a container needs. Every VibeSys module that
needs a provider fact reads it through here, at call time, so a library
upgrade changes behaviour without a VibeSys edit and tests have one place to
substitute a profile.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import agentshim

if TYPE_CHECKING:
    from agentshim import ProviderProfile


def provider_profile(provider: str) -> ProviderProfile:
    """Return the agentshim profile for *provider*.

    Raises:
        ValueError: if agentshim does not register *provider*. The message
            names the provider and the registered alternatives.
    """
    return agentshim.get_provider(provider).profile
