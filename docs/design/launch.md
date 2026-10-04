# Launching runs

LAUNCH-1 implements D206 and D210: `launch` owns VibeSys's built-in catalog
and `default_runs(settings) -> Runs`. Only entrypoints, tests and scripts
import it. `vibesys.api` owns the role contracts:

```python
class Runs(Protocol):
    def start(self, request: RunRequest) -> RunHandle: ...
    def resume(self, request: RunRequest) -> RunHandle: ...
    def attach(self, run_id: str) -> RunHandle: ...
    def list_active(self) -> tuple[RunHandle, ...]: ...

class RunHandle(Protocol):
    run_id: str
    def start(self) -> None: ...
    def events(self) -> AsyncIterator[CoreEvent]: ...
    def stop(self) -> None: ...
    async def result(self) -> RunResult: ...
```

Start and resume run inside an active event loop and return an already-started
handle. Handle start is idempotent and creates the execution task. Events are
semantic facts, replayed from the beginning to each subscriber. Result waits
for the independent task, and stop sends the existing run-control request.
The initial registry and attachment are process-local. Completed handles stay
attachable but are excluded from `list_active`.

`RunSession` (`api/session.py:52`) is the nearest existing type, but its
`start()` only subscribes and `await_result()` executes. The new handle replaces
that split execution ownership. A transitional session capability retains
server queries, readiness and auxiliary-agent construction until those roles
are extracted.

The audit's move list (paths under `src/`):

| Current construction | New owner |
| --- | --- |
| `vibesys/plugin_builtins.py`, catalog mapping in `plugin_catalog.py` and `plugin_registration.py` | `launch` catalog; pure registration/projection contracts remain core |
| `vibesys/api/_session.py:77,112,147,162`, `vibesys/composition.py:68-251` | `launch` default wiring and injected core session construction |
| `vibesys/api/request.py:104-148`, `vibesys/api/_store.py:137` | explicit catalog selection from launch wiring |
| `headless/execute.py:129`, `entrypoints/cli/loops.py:382` | entrypoints start a handle; headless renders it |
| `headless/execute.py:24-110`, `server/runtime.py:156-242` | entrypoints process signals and exit codes |
| `server/runtime.py:139-229`, `entrypoints/server.py:616` | injected `Runs`, server consumes handles |

Generic task, stream and registry mechanisms live in `vs-runtime` without
VibeSys imports. Launch connects concrete implementations to core contracts.
This replaces caller-owned execution rather than adding another lifecycle
state machine. Existing orchestration retains durable state and stop policy.

Deferred: D207 server lifecycle inference, chat's auxiliary construction
capability (`server/chat/factory.py:75,238,250`), configuration splitting,
startup recovery and durable cross-process attachment. No lifecycle authority
moves into the generic launcher.
