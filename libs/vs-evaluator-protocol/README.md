# vs-evaluator-protocol

## Responsibility

This package validates evaluator record streams and returns typed measurements to
the framework. Evaluator execution, file I/O, scoring, and the evaluator-side SDK
belong outside it. The protocol definition and SDKs live in `sdk/vs-evaluator/`.

## Concepts

The public API has three groups:

- `Hello`, `Result`, `ErrorRecord`, `MetricSpec`, and the `Record` union define
  the records, with `PROTOCOL_VERSION` naming the version this reader
  implements. `MetricSpec.required` (default `true`) says whether every
  successful run must report the metric; an optional metric is still declared,
  so it can never be reported under a name the reader has not seen.
- `parse_records` turns stream text into typed records, one per non-blank line.
  `read_measurement` consumes records in order and returns one `Measurement`:
  the declared metrics plus either the measured row or a failure message. An
  `error` may replace the `hello` entirely, for an evaluator that fails before
  its configuration tells it what it measures; such a `Measurement` has
  `metrics is None`. A measured row always carries the declaration it was
  validated against.
- `check_objectives` verifies that a task's objectives are declared by the
  evaluator *and* declared required. It is separate from `read_measurement`
  because objectives belong to the task; the evaluator never learns which
  metrics are optimized. A task cannot rank on an optional metric, since a
  successful run may omit it and the frontier would silently lose the round.

Every rejection raises `ProtocolError` with a `ReasonCode` and a message
naming the offending record and key. Record models reject unknown keys and
type coercion.

Version 1 streams remain accepted during the transition to version 2. The
reader treats every declared metric in a version 1 stream as required.

## Usage

Read an evaluator's output after the application has run it:

```python
from vs_evaluator_protocol import parse_records, read_measurement

measurement = read_measurement(parse_records(output_text))
```

The caller checks task objectives against the evaluator declaration with
`check_objectives` before using measurements for ranking.
