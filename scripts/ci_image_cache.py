"""Content-addressed registry references for CI's Docker image/build-cache snapshots."""

from __future__ import annotations

import argparse
import hashlib
import re

_REPOSITORY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*/[a-zA-Z0-9][a-zA-Z0-9_.-]*")


def image_cache_reference(repository: str, key: str) -> str:
    """Name a snapshot by all install inputs, independent of GitHub's run/ref identity.

    Workflow keys include the base digests and install source hashes. Hash the
    complete key to fit OCI's tag length limit without losing any input. A
    family/base-only key names a mutable fallback; Docker still checks every
    layer's actual inputs before reusing it.
    """
    if _REPOSITORY.fullmatch(repository) is None:
        message = f"invalid GitHub repository: {repository!r}"
        raise ValueError(message)
    if not key.strip():
        message = "image cache key must not be empty"
        raise ValueError(message)
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"ghcr.io/{repository.lower()}/ci-image-cache:{digest}"


def main(argv: list[str] | None = None) -> None:
    """Print the registry reference for the workflow's validated repository and key."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("key")
    args = parser.parse_args(argv)
    print(image_cache_reference(args.repository, args.key))


if __name__ == "__main__":
    main()
