"""Find the hosted runner's Ubuntu archive URI for Feature conformance builds."""

from __future__ import annotations

import logging
import re
from pathlib import Path

# Accept both deb822 URIs and traditional deb lines, without copying the
# runner's release, components, signing keys or third-party repositories.
_LOGGER = logging.getLogger(__name__)
_ARCHIVE = re.compile(r"https?://[a-zA-Z0-9.-]+(?::[0-9]+)?/ubuntu/?(?=\s|$)")


def ubuntu_mirror(sources: str) -> str | None:
    """Return the first Ubuntu archive URI, or None to keep the image defaults."""
    for line in sources.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in _ARCHIVE.finditer(line.split("#", 1)[0]):
            uri = match.group().rstrip("/")
            if uri not in {
                "http://security.ubuntu.com/ubuntu",
                "https://security.ubuntu.com/ubuntu",
            }:
                return uri
    return None


def runner_ubuntu_mirror(apt: Path) -> str:
    """Read runner source formats and its mirror list; warn and keep defaults if absent."""
    paths = [
        apt / "sources.list",
        *sorted((apt / "sources.list.d").glob("*.list")),
        *sorted((apt / "sources.list.d").glob("*.sources")),
        # runner-images uses mirror+file:/etc/apt/apt-mirrors.txt in both formats.
        apt / "apt-mirrors.txt",
    ]
    mirror = ubuntu_mirror("\n".join(path.read_text() for path in paths if path.is_file()))
    if mirror is None:
        _LOGGER.warning(
            "Runner apt sources contain no Ubuntu archive mirror; keeping image defaults"
        )
    return mirror or ""


if __name__ == "__main__":
    print(runner_ubuntu_mirror(Path("/etc/apt")))
