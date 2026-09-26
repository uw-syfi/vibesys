# Python

Runner: pytest, run through `uv`. Property testing: Hypothesis.

## Mapping the rules

| Rule | Python |
| --- | --- |
| Public API | `<pkg>.api` for a `libs/` package (tach enforces it); the module API siblings already import inside `vibesys`; `vibesys.api` at the top |
| No patching | Banned: `monkeypatch.setattr`, `setitem`, `delattr`, `delitem`, and `unittest.mock` / `pytest-mock` (`patch`, `MagicMock`, `mocker`). Allowed: `monkeypatch.setenv`, `delenv`, `chdir`, `tmp_path` |
| Fakes | Exemplar: `libs/vs-agent/src/vs_agent/fake_client.py` (`FakeAgentClient`) |
| Exemption | `# test-isolation: <reason>` on the site's line or the line above |

## Contract suite wiring

Parametrize one test over the Fake and the real implementation. The real
parameter carries the `real_contract` marker, which is opt-in with
`VIBESYS_REAL_CONTRACTS=1` (same shape as the `e2e` marker in `pyproject.toml`).

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

- The root `conftest.py` registers three profiles, all with `deadline=None`
  (Hypothesis's deadline is a wall-clock dependence), so do not set a deadline
  on a property. `ci` is derandomized and is selected automatically when `CI`
  is set, so CI runs are a pure function of the code. `dev` is the local
  default. `explore` is randomized with 500 examples; run it by hand to hunt
  for new bugs: `HYPOTHESIS_PROFILE=explore uv run pytest path/to/test.py`.
- Pin a found failure with `@example(...)`, so it reproduces under the
  derandomized `ci` profile too.
- Build strategies from the public Pydantic models and enums.

## Golden fixtures

Prompt snapshots live under `tests/vibesys/loops/*/fixtures/prompt_snapshots`.
Regenerate with `UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest <file>`, then read the
diff.

## Commands

```bash
uv run pytest path/to/test.py -k name              # narrowest first
uv run python scripts/check_test_isolation.py      # ratchet; --write only lowers counts
for i in $(seq 20); do uv run pytest path/to/test.py -q -p no:cacheprovider --no-cov || break; done
uv run pytest path/to/test.py -n auto --no-cov -q  # parallel
```

`scripts/check_test_isolation.py` counts patching, mocking, sleeps
(`time.sleep`, `asyncio.sleep` other than `asyncio.sleep(0)`), and
non-`api` imports of a library inside its own tests. The baseline is
`tests/quality/isolation_baseline.jsonl`. CI runs with `-n auto --dist loadgroup`; the
`serial` marker is a last resort for a host-wide resource.
