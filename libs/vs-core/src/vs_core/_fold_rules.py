"""The identity of the rules that fold a log: a digest of their source files.

A cache of a fold (``state.pb``) is the state those rules left after the log's first records. It is
valid for the log's bytes only under the rules that wrote it: a process whose reducers changed since
(a fix between a pause and the Resume) would fold the records after the cache with new rules over
state the old ones made, and get a state its own full fold would not. The cache therefore names the
rules it was folded by, and a start that finds other rules folds the whole log.

Core is pure, so it does not read its own source: the shell reads the files of ``vs_core`` and
names their digest (``vs_runtime``), and core only compares the name a fold carries with the one it is
read under.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def rules_digest(sources: Mapping[str, bytes]) -> str:
    """The identity of rules whose source files are ``sources``, by path relative to their package.

    It depends on every path and every byte, and not on the order the files were read in. No files
    is no identity: the empty string, which a fold is never read under.
    """
    if not sources:
        return ""
    digest = hashlib.sha256()
    for path in sorted(sources):
        content = sources[path]
        digest.update(f"{path}\0{len(content)}\0".encode())
        digest.update(content)
    return digest.hexdigest()
