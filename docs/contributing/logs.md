# Log streams

The older Python engine records output from image builds, container starts,
evaluator stages, tool builds and run narration as log streams. These streams
are for humans. Facts used to make decisions travel through typed events and
records; product code never parses its own narration to recover a fact.
Parsing a foreign tool's output belongs at that tool's call boundary.

The current desktop does not expose this log-store reader. Its live agent
conversation comes from the orchestrator's typed services instead.

## Ownership

Producers write bytes to an output sink. They know neither the storage layout
nor the run model. The log library owns streams, offsets, retention and the
index. The product assigns a typed owner describing what a stream is for.
A reader lists streams and reads bounded chunks without depending on a UI.

Libraries take output openers in their own vocabulary. Run assembly supplies
those openers and owns the store's lifetime. This keeps the log library above
infrastructure libraries in the dependency graph: infrastructure writes through
sinks and never imports the recorder or product owner types.

Every runner call explicitly chooses an output sink. A probe can discard output
when its caller only needs a bounded result; long-running work can give the
output a durable owner. The type checker rejects calls that make no choice.

## Writing and failure

A command opens a stream and closes it when it finishes or fails. Closing the
run's resources closes its store. Reopening a store marks streams left open by
a crashed writer as abandoned.

Logging failures must not fail the work being logged. A write that cannot be
stored degrades the stream, drops subsequent bytes and warns once. Closing
records the loss when possible. Writes after close are dropped. A store that
cannot open leaves the run with bounded in-memory output and a warning.

Append-only metadata tolerates a torn final record. Opening the writer removes
the torn suffix before appending; readers ignore incomplete trailing records.
A complete record that cannot decode is corruption and is reported as such.

## Offsets and retention

Every byte has a monotonically increasing offset. Retention evicts whole old
segments without renumbering retained bytes. A bounded read reports when the
requested beginning has already been evicted, identifies the output pipes and
says whether the stream has closed and the final byte has been reached.

The reader observes close state before reading bytes, so a closed stream at
its end cannot later grow. Degradation is separate from completion: a command
can finish successfully even though its diagnostic output was lost.

Remote evaluator output is copied through the runner's transport during the
existing scheduler polls, with bounded reads. Collection supplies any remaining
stage bytes. This adds no independent polling loop. Truncation or replacement
of a stage file closes the affected stream as failed.

## Narration

Run narration is a structured stream that preserves severity, source and
multiline tracebacks for a human renderer. The handler buffers a bounded amount
before a sink is attached, and a sink failure disables that handler with a
warning rather than raising into product code.

Output before run assembly or after resource closure cannot enter the run's
narration stream. A hard process death may also leave no final diagnostic.
Logs therefore complement the durable run record; they cannot establish that
an effect completed or that a result is trustworthy.

## Verification and limits

The file store and in-memory store pass the same public contract suite. Fault
tests cover full storage, corrupt metadata and failures at producer boundaries.
Dependency checks keep product owner types out of infrastructure libraries.
Whether code parses human text as a fact still requires review.

Some producers still discard output, and older narration files remain alongside
streams. Library reader tests cover bounded reads and retention; there is no
product consumer of that reader. Adding a view requires typed ownership and
explicit handling of truncation, degradation and abandoned streams.
