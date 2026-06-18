# Objective - KV store default harness

Maximize throughput (operations/second) of a key-value store under the
workload defined by the active scenario, while satisfying the correctness
invariants defined in issue #26.

## Scenarios

| Scenario    | Description                                               |
|-------------|-----------------------------------------------------------|
| point       | Mixed put/get/delete on individual keys (balanced load)   |
| scan        | Point ops plus prefix-range scans over ordered keys       |
| heavy-write | Write-dominated: high put/delete rate, minimal reads      |
| read-heavy  | Read-dominated: high get rate, infrequent puts            |

## API Contract (issue #26)

    put(key: str, value: bytes) -> bool      # True on success
    get(key: str) -> bytes | None            # None if key absent
    delete(key: str) -> bool                 # True if key existed
    size() -> int                            # current key count
    stats() -> dict                          # implementation-defined metrics

## Consistency Model

- A successful put makes the value visible to later get calls for the same key.
- A successful delete removes the key; subsequent get must return None.
- delete on a non-existent key returns False and is a no-op.
- Failed operations must not modify visible state.

## Notes

- Reference uses a threading.Lock-protected dict.
- Keys are UTF-8 strings. Values are opaque bytes.
- No hardware accelerator required; CPU-only.
- scan scenario requires an additional scan(prefix) method.
