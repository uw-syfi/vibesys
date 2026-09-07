# vs-correctness

`vs-correctness` is the user-facing framework for defining microservice fuzzing
and correctness gates. Users implement a `Generator` that returns immutable,
serializable `TestCase` values and an `Oracle` that returns `PASS`, `FAIL`, or
`INCONCLUSIVE`. `Verifier` executes the exact same case against an optional
baseline and the candidate, then supplies their separate observations to the
oracle.

The initial transport is `HTTPExecutor`. `HTTPAction` supports methods, paths,
repeated query values, headers, JSON or text bodies, and per-request timeouts.
`CustomAction` is the serializable escape hatch for other transports and fault
operations. The public executor protocol lets users interpret it without
changes to generation or oracle code. The built-in HTTP executor fails closed
when given a custom action.

For example, save this absolute correctness suite as `inventory_correctness.py`:

```python
from vs_correctness import (
    Decision, GenerationContext, HTTPAction, OracleContext, Suite, TestCase,
    Verdict,
)


class InventoryGenerator:
    def generate(self, context: GenerationContext):
        for index in range(context.cases):
            item_id = context.integer(1, 10_000)
            yield TestCase(
                id=f"inventory-{index}",
                actions=(HTTPAction(method="GET", path=f"/items/{item_id}"),),
            )


class InventoryOracle:
    def check(self, context: OracleContext) -> Decision:
        result = context.candidate.action_results[-1]
        if result.status == 404:
            return Decision(verdict=Verdict.PASS, reason="unknown item")
        if result.status != 200:
            return Decision(verdict=Verdict.FAIL, reason=f"status {result.status}")
        body = result.json_body()
        action = context.test_case.actions[0]
        expected_id = int(action.path.rsplit("/", 1)[-1])
        verdict = Verdict.PASS if body.get("id") == expected_id else Verdict.FAIL
        return Decision(verdict=verdict, reason="response id must match request")


SUITE = Suite(generator=InventoryGenerator(), oracle=InventoryOracle())
```

This is an absolute oracle, so it evaluates the candidate without a baseline.
A differential oracle instead requires `context.baseline`, normalizes both
observations as needed, and compares it with `context.candidate`. The verifier
still sends the same serialized `TestCase` to both environments.

```python
from vs_correctness import (
    Decision, Environment, HTTPAction, HTTPExecutor, Suite, TestCase,
    Verdict, Verifier, gate_exit_code, write_report,
)

report = Verifier(HTTPExecutor(), reset=reset_environment).verify(
    Suite(generator=my_generator, oracle=my_oracle),
    candidate=Environment(name="candidate", base_url="http://localhost:5000"),
    baseline=Environment(name="baseline", base_url="http://localhost:5001"),
    seed=42,
    cases=100,
)
write_report(report, "correctness-report.json")
raise SystemExit(gate_exit_code(report))
```

Suites that do not need custom lifecycle hooks can use the CLI directly:

```bash
python -m vs_correctness --suite inventory_correctness:SUITE \
  --candidate-url http://localhost:5000 --seed 42 --cases 100 \
  --report correctness-report.json
```

To replay a serialized `TestCase` (for example, a report's
`minimized_case` saved as `failing-case.json`):

```bash
python -m vs_correctness --suite inventory_correctness:SUITE \
  --candidate-url http://localhost:5000 --replay failing-case.json \
  --report replay-report.json
```

The resulting command is suitable for VibeSys `[accuracy].command`: the
existing framework gate accepts only exit code zero and rejects mutation of
evaluator-owned inputs. Oracle exceptions, reset failures, transport failures,
and `INCONCLUSIVE` verdicts produce a nonzero exit. An empty generated suite is
rejected.

Reports contain the seed, serialized cases, candidate and baseline revisions
and configuration, independent observations, verdicts, and minimized failing
cases. `load_test_case` and `Verifier.replay` rerun a persisted case. Reports
are written atomically.

`TestCase` deliberately contains only executable fuzz inputs: `id`, `setup`,
`actions`, and `cleanup`. Arbitrary metadata is rejected during validation, so
a generator cannot pass an expected response to the oracle through the case
contract. The oracle derives expected outcomes from the declared actions and
its own model. `id` is for reporting and is not an expectation channel.
`CustomAction.payload` is executable input for a custom executor; framework
users must keep expected outcomes out of it. Python cannot inspect user code to
enforce that semantic restriction.

Reports use schema version 2. Version 1 cases that contain `metadata` fail
strict replay validation instead of silently discarding their expectations.

Shrinking removes contiguous chunks from `actions`, retains setup and cleanup,
and has a fixed attempt limit. Every attempt calls the supplied reset hook and
keeps a reduction only when a fresh run returns `FAIL`. Infrastructure errors
become `INCONCLUSIVE`, so they cannot be mistaken for a smaller semantic
counterexample. Exact replay still depends on the user's reset hook restoring
external state and on the service controlling its own sources of
nondeterminism.

Before calling an oracle, the verifier also requires a successful executor
observation to identify the requested environment and contain exactly one
result for every declared setup, action, and cleanup position in order.
Misaligned observations are `INCONCLUSIVE` and cannot pass the gate.

`Reference` can place a prior status, first header value, or JSON path value in
an HTTP query value, header value, or the whole body. Nested references inside
a JSON request body are deferred.

This library owns case execution, observations, replay, bounded structural
shrinking, and gate-safe reporting. Application schemas, valid input grammars,
state models, normalization choices, deployment, readiness, and reset logic
remain user-owned. Distributed schedules, fault injection, automatic
dependency-aware shrinking, and built-in service lifecycle management are
deferred.
