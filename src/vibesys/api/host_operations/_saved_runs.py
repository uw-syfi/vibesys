"""One scan of a project's saved runs, as the frames of one stream.

The caller's rows arrive in discovery order from a single scan, so a frame costs its own rows and
nothing is rescanned. A frame is how the gateway knows the call is alive, and the last one says the
listing is complete: a stream that ends any other way is partial, whatever it delivered.
"""

from collections.abc import Callable, Iterable, Iterator

from vibesys.api.host_operations._login import frame
from vibesys.api.host_operations._reads import ReadSource
from vs_proto.api import (
    FailureCategory,
    Oneof,
    OperationFailure,
    ValidationError,
    WatchRequest,
    WatchResponse,
    validate,
)
from vs_proto.api import host_payloads as pb

_MAX_BYTES = 1 << 20
_MAX_DIAGNOSTIC = 65536
_MAX_ROWS = 200
_ROW_FRAMING_BYTES = 4
"""A row's field tag and length, which its own encoding leaves out."""
_MAX_ROW_BYTES = _MAX_BYTES - 16384
"""What a frame's rows may take, leaving room for its identity, its root and the envelope."""


def saved_run_frames(
    request_id: str,
    project_root: str,
    rows: Iterable[pb.SavedRun],
    *,
    max_rows: int = _MAX_ROWS,
) -> Iterator[pb.StreamSavedRunsResponse]:
    """``rows`` as frames of at most ``max_rows`` rows and 1 MiB; only the last says complete."""

    def built(runs: list[pb.SavedRun], *, complete: bool) -> pb.StreamSavedRunsResponse:
        return pb.StreamSavedRunsResponse(
            request_id=request_id, project_root=project_root, runs=runs, complete=complete
        )

    batch: list[pb.SavedRun] = []
    size = 0
    for row in rows:
        length = len(row.to_binary()) + _ROW_FRAMING_BYTES
        if len(batch) == max_rows or size + length > _MAX_ROW_BYTES:
            yield built(batch, complete=False)
            batch, size = [], 0
        batch.append(row)
        size += length
    yield built(batch, complete=True)


def host_watch_failure(category: FailureCategory, message: str) -> bytes:
    """A framed failure. Callers branch on the category, not on the text."""
    text = message.strip() or "host watch failed"
    encoded = text.encode()
    if len(encoded) > _MAX_DIAGNOSTIC:
        text = encoded[:_MAX_DIAGNOSTIC].decode(errors="ignore") or "host watch failed"
    failure = OperationFailure(category=category, message=text)
    return frame(WatchResponse(result=Oneof("failure", failure)).to_binary())


def serve_saved_runs(
    payload: bytes,
    source: ReadSource,
    *,
    cli: str,
    write: Callable[[bytes], object],
) -> None:
    """Write one project's runs as frames, then the frame that says the listing is complete.

    A row the schema refuses fails the listing at that frame; frames already written stand.
    """
    if cli != "stream-saved-runs":
        write(
            host_watch_failure(
                FailureCategory.INVALID_INPUT, "watch command does not match the envelope"
            )
        )
        return
    if len(payload) > _MAX_BYTES:
        write(host_watch_failure(FailureCategory.INVALID_INPUT, "HostWatch request exceeds 1 MiB"))
        return
    try:
        request = WatchRequest.from_binary(payload)
        validate(request)
    except (ValidationError, ValueError) as exc:
        write(host_watch_failure(FailureCategory.INVALID_INPUT, str(exc)))
        return
    method = request.method
    if method is None or method.field != "stream_saved_runs":
        write(host_watch_failure(FailureCategory.INVALID_INPUT, "HostWatch.method is required"))
        return
    listing = method.value
    try:
        root, rows = source.saved_runs(listing.project)
        for response in saved_run_frames(listing.request_id, root, rows):
            validate(response)
            write(frame(WatchResponse(result=Oneof("stream_saved_runs", response)).to_binary()))
    except (ValidationError, ValueError, TypeError) as exc:
        write(host_watch_failure(FailureCategory.INVALID_INPUT, str(exc)))
