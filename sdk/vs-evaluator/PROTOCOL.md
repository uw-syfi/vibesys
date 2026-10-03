# VibeSys evaluator result protocol

Version 2.

An evaluator reports its measurements to the VibeSys framework as a **record
stream**: a sequence of JSON objects, one per line, written to the file named by
the evaluator's output flag. The framework reads the stream after the process
exits. A future transport may deliver the same records over a live connection;
the record definitions below do not depend on the transport.

The evaluator declares the metrics it produces. It never learns which of them a
task optimizes for: objectives live in the task's `vibesys.input.toml` and are
checked against the declared metrics by the framework.

## Records

Each line is one JSON object with a `kind` discriminator. Unknown `kind` values
are rejected. Unknown keys inside a known record are rejected.

### `hello`

Exactly one. It must be first, unless the stream is a single `error` (see
below).

```json
{"kind":"hello","protocol":2,"metrics":{"total_ops_per_sec":{"unit":"ops/s","direction":"max"}}}
```

- `protocol` (int, required): protocol version. Readers reject versions they do
  not implement.
- `metrics` (object, required, non-empty): the metric names this evaluator
  produces. Each value is a spec object:
  - `unit` (string, optional): human-facing unit. Never parsed for meaning.
  - `direction` (`"max"` or `"min"`, optional): intrinsic better-direction of
    the metric. Advisory metadata. It does not select objectives. Only these two
    spellings are valid on the wire. An SDK may accept `maximize`/`minimize` at
    its own API boundary, since that is how task configs spell them, but must
    write the short form.
  - `required` (bool, optional, default `true`): whether every successful run
    must report this metric. Producers omit the key when a metric is required,
    since the corpus compares records semantically and an explicit `true` would
    not match its counterpart. Mark a metric optional when the evaluator can only
    sometimes produce it, for example a latency distribution that is empty when
    no sample matched. An optional metric is still declared, so it can never be
    reported under a name the reader has not seen.

Metric names must be non-empty and contain no whitespace.

### `result`

Zero or one per stream. A stream with no `result` and no `error`
is invalid.

```json
{"kind":"result","label":"","values":{"total_ops_per_sec":41250.3}}
```

- `label` (string, optional, default `""`): names the operating point. Reserved
  for future multi-point streams; version 2 readers accept only the default.
- `values` (object, required): the measured row. Every name in it must be
  declared in `hello`, and every metric `hello` marked required must appear.
  An optional metric may be absent. Every value must be a finite JSON number.
  Booleans are not numbers.

### `error`

Terminates the stream. Any `result` in the same stream is ignored.

An `error` may appear without a preceding `hello`, and is the only record that
may. An evaluator whose metric identity comes from configuration cannot declare
anything until that configuration loads, and a failure before then still needs
to reach the framework as a reason rather than as a missing file.

Precisely: an `error` in the first position is accepted whether or not a `hello`
ever arrives, and, as with any `error`, later records are not checked. A `hello`
that arrives after any other record is still `HELLO_NOT_FIRST`, and a stream
that reaches its end with neither a first-position `error` nor a `hello` is
`MISSING_HELLO`.

```json
{"kind":"error","message":"vLLM server did not become ready within 300s"}
```

- `message` (string, required, non-empty).
- `partial` (object, optional): what the run measured before it failed or was
  stopped. It never counts as a result; it lets a reader compare failed runs by
  how close each came. Omit it when nothing was measured; never estimate it.
  - `name` (string, required, non-empty, no whitespace): the measured quantity.
    It need not be a metric declared in `hello`: a run cut short usually
    measures a different quantity (for example a warmup phase's rate).
  - `value` (finite number, required): the measured value.
  - `direction` (`"max"` or `"min"`, required): which way is better.
  - `unit` (string, optional, non-empty).
  - `target` (finite number, optional): the value a passing run needs.
  - `progress` (object, optional): `completed` (integer, at least 0) units of
    work out of `required` (integer, at least 1), and `unit` (string, no
    whitespace) naming one unit of work.

```json
{"kind":"error","message":"warmup stopped at 120s","partial":{"name":"warmup_output_tokens_per_s","value":16.4,"direction":"max","unit":"output tokens/s","target":79.7,"progress":{"completed":15,"required":72,"unit":"rounds"}}}
```

Unknown keys inside `partial` and `progress` are rejected like any other
unknown key. `partial` was added to protocol 2 as an optional key, so a stream
without it is unchanged.

## Serialization

- One record per line, UTF-8, newline-terminated.
- Blank lines are ignored. Trailing newline optional.
- Records are compared semantically, not byte for byte. Implementations may
  differ in key order and float formatting as long as the parsed records match.

## Reader obligations

A conforming reader must reject a stream when any of these hold, and must name
the offending record and key in its error:

1. no `hello`, or `hello` is not first, or more than one `hello`. A stream
   whose only record is an `error` is the one exception
2. `protocol` is not a version the reader implements
3. `metrics` is empty, or a metric name is empty or contains whitespace
4. neither `result` nor `error` is present
5. `values` has a key not in `metrics`, or is missing a metric `metrics` marked
   required
6. a value is not a number, is a boolean, or is not finite
7. `label` is not `""`
8. a record has an unknown `kind`, or a known record has an unknown key
9. a line is not a JSON object
10. a second `result` appears. Protocol 2 carries one operating point, so a
    second row is a producer bug, not a value to silently overwrite.

Rules 1 through 9 are per-record and can be applied as records arrive. Rule 1
splits by position: a `hello` at any position after the first is
`HELLO_NOT_FIRST`, reported immediately; the absence of a `hello` is
`MISSING_HELLO`, and can only be reported once the stream ends, since an
`error`-only stream is valid and is not distinguishable until then. A reader may
therefore not fail on the first offending record in that one case.

An `error` record terminates the stream. Records after it are not checked.

### Reason codes

Every rejection carries one of these codes. The codes are part of the contract;
the wording of the message is not. `fixtures/expectations.json` binds each
invalid fixture to the code it must produce.

`MISSING_HELLO`, `HELLO_NOT_FIRST`, `DUPLICATE_HELLO`, `UNSUPPORTED_PROTOCOL`,
`EMPTY_METRICS`, `INVALID_METRIC_NAME`, `NO_OUTCOME`, `UNKNOWN_METRIC`,
`MISSING_METRIC`, `NON_NUMERIC_VALUE`, `NON_FINITE_VALUE`, `UNSUPPORTED_LABEL`,
`UNKNOWN_KIND`, `UNKNOWN_KEY`, `MALFORMED_LINE`, `INVALID_RECORD`.

Three of these need their boundaries stated:

- `UNKNOWN_KIND` covers an unrecognized `kind`, a missing `kind`, and a `kind`
  that is not a string. All three mean the reader cannot tell what the record is.
- `UNKNOWN_KEY` is only for a key that is not part of the record's definition.
- `INVALID_RECORD` is every other record-level failure: a wrong field type, a
  blank `error.message`, a second `result`.

## Objectives

The framework applies two further checks outside the protocol, because
objectives belong to the task and the evaluator never learns them:

1. Every objective named by the task must appear in `hello.metrics`.
2. Every objective must be declared `required`. An optional metric may be
   absent from a successful run, so a task cannot rank on one: the frontier
   would silently lose the round, which is the failure this protocol exists to
   prevent.

Both reuse `MISSING_METRIC`, naming the offending objectives and listing what
the evaluator declared.

## Version 2 limits

**One row per stream.** `label` exists so a later version can carry several
operating points, but version 2 readers accept only `""`. This is deliberate:
the framework has no model for multi-operating-point measurements, so putting
labelled rows on the wire would move the problem rather than solve it. An
evaluator with a genuine multi-point mode should reject the combination of that
mode and a requested stream, rather than silently reporting one of the points.
The queue evaluator does exactly this for `--scenario all`.

**Write `hello` before measuring, not at the end.** Producers should write and
flush `hello` as soon as the schema is known. If the evaluator then crashes or
is killed by a timeout, the stream holds a schema and no outcome, which a reader
reports as `NO_OUTCOME`: "it started and died". Deferring `hello` to the end
would make that indistinguishable from `MISSING_HELLO`, which means "this
producer does not speak the protocol". The two need different fixes, so they
need different reports.

## Fixtures

`fixtures/valid/` holds streams every reader must accept and every SDK must be
able to produce. `fixtures/invalid/` holds streams every reader must reject, with
the expected reason recorded in `fixtures/expectations.json`. The corpus is the
specification; this document describes it.
