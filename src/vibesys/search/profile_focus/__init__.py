"""Pure profile-guided component-focus search policy.

Ports the deterministic logic from ``loops/profile_multi/controller.py``
(``ProfileGuidedHypothesisController`` and friends) and the pure half of
``loops/profile_multi/attribution.py``. See
:class:`~vibesys.search.profile_focus.focus.ProfileFocus` for the public
entry point. Independent of ``vibesys.search.hypothesis``; orchestration
composes the two.
"""

from __future__ import annotations

from vibesys.search.profile_focus.attribution import (
    ProfileAttributionError,
    parse_attribution,
)
from vibesys.search.profile_focus.config import ProfileFocusConfig
from vibesys.search.profile_focus.focus import ProfileFocus
from vibesys.search.profile_focus.results import FocusView
from vibesys.search.profile_focus.state import (
    ProfileAttributionSample,
    ProfileBottleneck,
    ProfileFocusState,
    ProfileGuidanceStatus,
    ProfileGuidedComponent,
    ProfileImprovementSample,
)

__all__ = [
    "FocusView",
    "ProfileAttributionError",
    "ProfileAttributionSample",
    "ProfileBottleneck",
    "ProfileFocus",
    "ProfileFocusConfig",
    "ProfileFocusState",
    "ProfileGuidanceStatus",
    "ProfileGuidedComponent",
    "ProfileImprovementSample",
    "parse_attribution",
]
