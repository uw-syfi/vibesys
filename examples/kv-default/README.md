# KV Default Harness

Resolves #28.

Reusable reference implementations, correctness checkers, and benchmark
drivers for the four initial VibeServe KV store scenarios (issue #26).

## Running the correctness checker

    python accuracy_checker/checker.py --scenario all
    python accuracy_checker/checker.py --scenario point --ops 5000
    python accuracy_checker/checker.py --scenario heavy-write --writers 8

## Running the benchmark

    python benchmark/benchmark.py --scenario point --duration 10
    python benchmark/benchmark.py --scenario all --output-json results.json
    python benchmark/benchmark.py --scenario read-heavy --use-reference --readers 8

## Implementing a candidate

Create main.py in the harness root with a VibeServeKV class:

    class VibeServeKV:
        def __init__(self, scenario: str, **kwargs): ...
        def put(self, key: str, value: bytes) -> bool: ...
        def get(self, key: str) -> bytes | None: ...
        def delete(self, key: str) -> bool: ...
        def size(self) -> int: ...
        def stats(self) -> dict: ...
        def scan(self, prefix: str) -> list[tuple[str, bytes]]: ...  # scan scenario only

## Acceptance criteria (from #26)

- Each reference implementation passes its scenario correctness checker.
- The benchmark runs each scenario without modifying benchmark code.
- delete returns True only for existing keys; get after delete returns None.
- double-delete returns False.
