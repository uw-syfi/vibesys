"""Host reads and writes of an agent-controlled tree, walked by descriptor from its root.

Every host operation on a path the agent can change goes through a ``WorkspaceTree``
from :func:`open_workspace_tree`: a symlink, ``..`` or an absolute path on the way is
refused, so nothing the agent planted can redirect a host read, write or removal.
"""

from vs_project._workspace_tree import (
    CopiedTree,
    CopyIgnore,
    EntryKind,
    NotRegularFileError,
    WorkspacePathError,
    WorkspaceTree,
    open_workspace_tree,
    relative_parts,
)

__all__ = [
    "CopiedTree",
    "CopyIgnore",
    "EntryKind",
    "NotRegularFileError",
    "WorkspacePathError",
    "WorkspaceTree",
    "open_workspace_tree",
    "relative_parts",
]
