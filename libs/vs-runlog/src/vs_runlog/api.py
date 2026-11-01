"""Bounded append-only run.log files, with offset-based reads and explicit stop reasons."""

from vs_runlog._atomic import FakeFileSync, FileSync, OsFileSync
from vs_runlog._confirmed_log import (
    Appended,
    ConfirmedLog,
    CoreMessage,
    ProjectionWriter,
    RunLogRecords,
    SharedWriter,
    Uncertain,
)
from vs_runlog._epoch import CurrentEpoch, current_epoch, epoch_has_ended
from vs_runlog._file import (
    FileRunContent,
    FileRunLogWriter,
    RunLogReader,
    install_run_log,
    open_run_log,
)
from vs_runlog._file_fold_cache import FileFoldCache
from vs_runlog._fold import fold_skeleton
from vs_runlog._fold_cache import (
    CachedFold,
    CacheMiss,
    FoldCache,
    MemoryFoldCache,
    PrefixDigest,
    judge,
)
from vs_runlog._fold_cache_contract import CacheUnderTest, FoldCacheContract
from vs_runlog._framing import ReadStop, RunLogStopReason, encode_frame
from vs_runlog._head import project_head
from vs_runlog._lock import (
    RunLaunchLockedError,
    RunLock,
    WriterIdentityState,
    assert_run_unlocked,
    exclusive_run_launch,
    observe_writer_identity,
    writer_identity_path,
)
from vs_runlog._observe import (
    HeadObserver,
    RunLogDamagedError,
    read_committed_records,
    read_current_epoch,
    read_run_head,
)
from vs_runlog._prefix import CommittedPrefix, read_committed_prefix
from vs_runlog._upgrade import upgrade_skeleton

__all__ = [
    "Appended",
    "CacheMiss",
    "CacheUnderTest",
    "CachedFold",
    "CommittedPrefix",
    "ConfirmedLog",
    "CoreMessage",
    "CurrentEpoch",
    "FakeFileSync",
    "FileFoldCache",
    "FileRunContent",
    "FileRunLogWriter",
    "FileSync",
    "FoldCache",
    "FoldCacheContract",
    "HeadObserver",
    "MemoryFoldCache",
    "OsFileSync",
    "PrefixDigest",
    "ProjectionWriter",
    "ReadStop",
    "RunLaunchLockedError",
    "RunLock",
    "RunLogDamagedError",
    "RunLogReader",
    "RunLogRecords",
    "RunLogStopReason",
    "SharedWriter",
    "Uncertain",
    "WriterIdentityState",
    "assert_run_unlocked",
    "current_epoch",
    "encode_frame",
    "epoch_has_ended",
    "exclusive_run_launch",
    "fold_skeleton",
    "install_run_log",
    "judge",
    "observe_writer_identity",
    "open_run_log",
    "project_head",
    "read_committed_prefix",
    "read_committed_records",
    "read_current_epoch",
    "read_run_head",
    "upgrade_skeleton",
    "writer_identity_path",
]
