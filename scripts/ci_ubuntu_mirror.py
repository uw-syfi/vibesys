"""Find the hosted runner's Ubuntu archive URI for Feature conformance builds."""

from __future__ import annotations

import re
from pathlib import Path

# Accept both deb822 URIs and traditional deb lines, without copying the
# runner's release, components, signing keys or third-party repositories.
_ARCHIVE = re.compile(r"https?://[a-zA-Z0-9.-]+(?::[0-9]+)?/ubuntu/?(?=\s|$)")


def ubuntu_mirror(sources: str) -> str:
    """Return the first Ubuntu archive URI, rejecting a missing runner mirror."""
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
    msg = "runner apt sources contain no Ubuntu archive mirror"
    raise ValueError(msg)


if __name__ == "__main__":
    apt = Path("/etc/apt")
    paths = [
        apt / "sources.list",
        *sorted((apt / "sources.list.d").glob("*.list")),
        *sorted((apt / "sources.list.d").glob("*.sources")),
    ]
    print(ubuntu_mirror("\n".join(path.read_text() for path in paths if path.is_file())))
