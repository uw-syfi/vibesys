# accuracy_checker/checker.py

Loose quality gate that confirms the server is actually emitting code-edit output rather than prose, errors, or arbitrary unrelated text.

## What it asserts

Per-sample, with greedy decoding:

| Check | Default threshold | Why |
|:--|:--|:--|
| `ratio(output, gold) ≥ 0.50` | gold-rate ≥ 50% | The output is at least half-aligned with the reference fix at the character level — enough to rule out empty / off-task / corrupted responses. |

`ratio` is `difflib.SequenceMatcher(None, a, b).ratio()` — character-level. Markdown code fences in the model's output are stripped before scoring (the system prompt asks for no fences but some models add them anyway).

## Running

```bash
uv run python checker.py --url http://localhost:8000 --num-samples 10
# Exit 0 iff the gate passes (and no transport errors).
```

`--seed` defaults to `None` so each run pulls a fresh slice — over-fitting to a fixed 10-sample subset is impossible. Tune `--min-gold-similarity` and `--min-gold-rate` if you want a tighter or looser gate.

## What this gate does NOT cover

- **Functional correctness** (does the corrected code pass the dataset's
  hidden tests?). CodeEditorBench ships per-sample stdin/stdout test
  cases; running them is out of scope here.
- **Anti-bypass**. With only the gold-similarity gate, a server that
  echoes the buggy input verbatim will often pass — buggy programs in
  this dataset are typically very close to their fixes. If you need
  bypass detection, check the per-sample diff stats from
  `bench/benchmark.py` (token-level alignment vs. the prediction).
