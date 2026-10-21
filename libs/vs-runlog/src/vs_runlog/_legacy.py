"""Phase 1 bridge from already committed JSONL bytes to run.log records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from vs_proto.api import (
    API_SCHEMA_HASH,
    STORE_SCHEMA_HASH,
    EpochStarted,
    LegacyEvent,
    Oneof,
    Record,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from vs_proto.api import Timestamp
    from vs_runlog._file import FileRunLogWriter


class LegacyRunLogTee:
    """A single epoch's live tee, called only after the JSONL append succeeds."""

    def __init__(
        self,
        writer: FileRunLogWriter,
        run_id: str,
        timestamp: Callable[[], Timestamp],
        *,
        tree_hash: str = "",
        dirty: bool = False,
    ) -> None:
        """Own ``writer`` and commit this process's epoch header.

        The caller configures the writer's clock and owning event loop before
        handing it to the tee. The tee closes it on every exit.
        """
        self._writer = writer
        self._epoch = self._writer.epoch + 1
        self._timestamp = timestamp
        try:
            self._writer.append(
                Record(
                    seq=self._writer.sequence + 1,
                    at=timestamp(),
                    epoch=self._epoch,
                    body=Oneof(
                        "epoch_started",
                        EpochStarted(
                            run_id=run_id,
                            epoch=self._epoch,
                            tree_hash=tree_hash,
                            dirty=dirty,
                            api_schema_hash=API_SCHEMA_HASH,
                            store_schema_hash=STORE_SCHEMA_HASH,
                        ),
                    ),
                )
            )
        except BaseException:
            self._writer.close()
            raise

    def append(self, payload: bytes) -> None:
        """Append the exact journal JSON, without its newline delimiter."""
        try:
            self._writer.append(
                Record(
                    seq=self._writer.sequence + 1,
                    at=self._timestamp(),
                    epoch=self._epoch,
                    body=Oneof(
                        "legacy_event",
                        LegacyEvent(json=payload.removesuffix(b"\n").decode("utf-8")),
                    ),
                )
            )
        except BaseException:
            self.close()
            raise

    def commit(self) -> None:
        """Durably commit the pending legacy suffix at an explicit run boundary."""
        self._writer.commit()

    def close(self) -> None:
        """Release this epoch's append handle."""
        self._writer.close()

    def __enter__(self) -> Self:
        """Own this tee for a lexical conversion scope."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the writer on every exit."""
        self.close()
