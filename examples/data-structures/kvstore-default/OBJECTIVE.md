# Objective - KV store default harness

Maximize throughput (operations/second) for a concurrent key-value store while
preserving linearizable semantics for put/get/delete under mixed read/write load.

## Operations

| Operation | Description |
|-----------|-------------|
| put(key, value) | Set key to value, returns success |
| get(key) | Return value for key or None if missing |
| delete(key) | Remove key and return whether key existed |

## Notes

- Reference uses a Python dictionary protected by `threading.Lock`.
- Checker validates histories with Porcupine linearizability checking.
- No hardware accelerator required; CPU-only.
