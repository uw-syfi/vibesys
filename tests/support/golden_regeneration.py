"""The one command that rewrites the committed goldens a conversion or gateway change invalidates."""

from __future__ import annotations

#: Rewrites ``proto/testdata/conversion``, ``proto/testdata/skeleton-wire`` and the retained
#: gateway replies. The native replays are not goldens: they are generated, never committed.
REGENERATE = "./scripts/regenerate_goldens.sh"
