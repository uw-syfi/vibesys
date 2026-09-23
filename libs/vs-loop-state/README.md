# vs-loop-state

## Responsibility

This package defines validated loop history, progress, and archive values, with
JSON-compatible codecs and in-memory history behavior. `vs-project` owns state
persistence; VibeSys owns Git operations and loop orchestration.

## Concepts

The public API includes:

- `RoundRecord` defines one validated completed-round record.
- `serialize_round_record` and `parse_round_record` define its portable JSON
  representation.
- `RoundHistory` collects records in memory and resolves rollback bases.
- `PlainLoopCursor`, `PlainPerformanceRecord`, and
  `PlainPerformanceSnapshot` define versioned plain-loop state.
- `IndividualRecord` and `PopulationSnapshot` define a versioned evolve archive
  with validated IDs, lineage references, and finite fitness metrics.

Plain and evolve persisted models reject unknown fields and type coercion.
Performance timestamps require timezone information. The `serialize_*`
functions return JSON-compatible dictionaries, and matching `parse_*`
functions validate those dictionaries without reading files.

The library does not read or write files. VibeSys defines the application
vocabulary stored in these values.

## Usage

Codecs keep persisted values separate from file I/O:

```python
from vs_loop_state import PlainLoopCursor, parse_plain_loop_cursor, serialize_plain_loop_cursor

cursor = PlainLoopCursor()
restored = parse_plain_loop_cursor(serialize_plain_loop_cursor(cursor))
```
