"""A run's state as its run.log leaves it, for a process that is not running it.

The log is the one truth of a run: core's state is a fold of its records. A reader that holds no
search driver cannot replay the search's steps, so it takes the search's state from the last whole
state the log holds. The search logs one at a restart, as the log doubles, and when the run
parks or ends: exact for a run at rest, and as old as the last of them for a run still going.
The process running the run shows the live state through its commit observer instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from protobuf import Oneof

from vs_decode.api import Unreadable, decode_json
from vs_project.api import Project
from vs_proto.api import RUN_LOG_MAX_RECORD_BYTES
from vs_runlog.api import FileFoldCache, read_committed_records
from vs_runtime.api.step_runner import saved_run_in, start_fold

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreState, OperationRegistry, StrategyState
    from vs_proto.api import Record


@dataclass(frozen=True)
class LoggedRun[S: StrategyState]:
    """What a log shows of a run."""

    core: CoreState
    """The fold of the log's committed prefix."""
    strategy: S | None
    """The search's state at the last whole state the log holds; ``None`` before the first."""


def read_logged_run[S: StrategyState](
    project: Project, run_id: str, *, registry: OperationRegistry, state_type: type[S]
) -> LoggedRun[S] | None:
    """The run ``run_id`` as its log leaves it, or None while the log holds no start of it.

    Reads only: the cache beside the log is used when it covers the log's first records, and is
    never written.
    """
    log = Project.run_log_path_for(project.root, run_id)
    if not log.exists():
        return None
    records = read_committed_records(log)
    folded, _ = start_fold(
        FileFoldCache(Project.state_cache_path_for(project.root, run_id)), records, registry
    )
    if folded is None:
        return None
    return LoggedRun(folded.state, _last_whole_state(records, registry, state_type))


def _last_whole_state[S: StrategyState](
    records: Sequence[Record], registry: OperationRegistry, state_type: type[S]
) -> S | None:
    for record in reversed(records):
        encoded = _whole_state_of(record, registry)
        if encoded is not None:
            decoded = decode_json(
                encoded,
                state_type,
                source=f"the search state in record {record.seq} of the log",
                max_bytes=RUN_LOG_MAX_RECORD_BYTES,
            )
            if isinstance(decoded, Unreadable):
                raise ValueError(str(decoded))
            return decoded
    return None


def _whole_state_of(record: Record, registry: OperationRegistry) -> bytes | None:
    match record.body:
        case Oneof(field="strategy_advanced", value=advanced):
            match advanced.strategy:
                case Oneof(field="checkpoint", value=whole):
                    return whole
                case _:
                    return None
        case Oneof(field="core_snapshot"):
            saved = saved_run_in(record, registry)
            return None if saved is None else saved.strategy
        case _:
            return None
