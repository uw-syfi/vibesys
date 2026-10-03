# vs-loop-state

## Responsibility

This package defines validated hypothesis-search round history, with a
JSON-compatible codec and in-memory rollback behavior. `vs-project` owns state
persistence; VibeSys owns Git operations and orchestration policy.

## Concepts

The public API includes:

- `RoundRecord` defines one validated completed-round record.
- `serialize_round_record` and `parse_round_record` define its portable JSON
  representation.
- `RoundHistory` collects records in memory and resolves rollback bases.

The codec returns a JSON-compatible dictionary and validates it without
reading files.

The library does not read or write files. VibeSys defines the application
vocabulary stored in these values.

## Usage

Codecs keep persisted values separate from file I/O:

```python
from vs_loop_state.api import (
    RoundRecord,
    parse_round_record,
    serialize_round_record,
)

record = RoundRecord(
    round_number=1,
    commit=None,
    perf_metric=None,
    perf_unit=None,
    passed=True,
)
restored = parse_round_record(serialize_round_record(record))
```
