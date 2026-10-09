"""External-symlink mount and lifecycle helpers for sandbox composition."""

from vs_sandbox.symlink_mounts import (
    SymlinkMountScope,
    collect_symlink_mounts,
    find_mount_root,
    symlink_lifecycle_hooks,
)

__all__ = [
    "SymlinkMountScope",
    "collect_symlink_mounts",
    "find_mount_root",
    "symlink_lifecycle_hooks",
]
