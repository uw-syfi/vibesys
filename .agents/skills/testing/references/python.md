# Python

Python idioms for the testing rules, with pytest and Hypothesis.

## Mapping the rules

| Rule | Python |
| --- | --- |
| Public API | Import the package's declared public module (its `api` or `__init__` exports), never an underscore module or attribute |
| No patching | Banned: `monkeypatch.setattr`, `setitem`, `delattr`, `delitem`, and `unittest.mock` / `pytest-mock` (`patch`, `MagicMock`, `mocker`). Allowed: `monkeypatch.setenv`, `delenv`, `chdir`, `tmp_path` |
| Fakes | A class that implements the same `typing.Protocol` as production, shipped by the library that owns the protocol |
| Exemption | `# test-isolation: <reason>` on the site's line or the line above |

## Contract suite wiring

Parametrize the interface's suite over every Fake and production implementation.
Register every implementation factory. A production parameter that needs real
services carries a marker that is deselected unless an opt-in environment
variable is set.

```python
IMPLS = [
    pytest.param(make_fake, id="fake"),
    pytest.param(make_real, id="real", marks=pytest.mark.real_contract),
]

@pytest.mark.parametrize("make", IMPLS)
def test_missing_key_raises_the_documented_error(make):
    store = make()
    with pytest.raises(KeyMissing):
        store.get("absent")
```

For a differential test, use a Hypothesis `RuleBasedStateMachine`.

## Properties

- Register Hypothesis profiles once, in the root `conftest.py`, all with
  `deadline=None` (Hypothesis's deadline is a wall-clock dependence), and do
  not set a deadline on a single property.
- Make the pull-request profile `derandomize=True`, so a PR run is a pure
  function of the code, and keep a randomized profile with more examples for
  scheduled runs and bug hunts.
- Pin a found failure with `@example(...)`, so it reproduces under the
  derandomized profile too.
- Build strategies from the public Pydantic models and enums
  (`st.from_type`, `st.builds`, `st.sampled_from(MyEnum)`).

## Async and time

- Run async code under test on an event loop whose clock the test controls,
  so sleeping costs no wall time and a test that waits on nothing fails at
  once instead of hanging.
- Take `Clock` and `Sleeper` protocols as parameters in the code under test;
  never call `time.monotonic` or `asyncio.sleep` directly in code a
  deterministic test reaches.
- Run anything that changes signals, environment, working directory or timers
  in a child process the harness manages, not in the test process.

## Golden fixtures

Compare snapshots through one helper, and regenerate them only behind one
explicit environment variable or flag. Read the diff before committing it.

## Commands

```bash
pytest path/to/test.py -k name                    # narrowest first
pytest path/to/test.py -n auto                    # parallel, with pytest-xdist
for i in $(seq 20); do pytest path/to/test.py -q || break; done
```
