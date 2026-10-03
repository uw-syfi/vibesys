# Accuracy checker

`checker.py` boots `python3 server.py` from the workspace root (or uses
`--base-url`) and runs, with thinking disabled
(`chat_template_kwargs.enable_thinking = false`):

| Gate | Probes |
| --- | --- |
| Held-out multi-turn sessions (non-empty, sane finish reason, growing `prompt_tokens`) | 6 |
| History recall (fact from turn 1 asked back in turn 3) | 4 |
| Arithmetic sanity | 3 |
| Greedy-token pins vs. a reference forward | one aggregate check over all pins |

Exit 0 iff all pass. Flags: `--workspace`, `--model-path` (default
`$MODEL_PATH`), `--host`, `--port`, `--base-url`, `--pins`, `--output-json`,
`--skip-pins`.

## Pins

`reference/pins.json`:

```json
{
  "version": 1,
  "model": "<checkpoint used for the reference forward>",
  "n_tokens": 32,
  "enable_thinking": false,
  "pins": [
    {
      "id": "pin00",
      "messages": [{"role": "user", "content": "..."}],
      "expected_token_ids": [/* n_tokens greedy token ids */]
    }
  ]
}
```

The checker sends each pin's `messages` with `max_tokens = 32`, `temperature`
0, `ignore_eos`, thinking off, then compares against the first 32
`expected_token_ids`. The chat API returns text, not ids, so the checker
retokenizes the returned text with the tokenizer at `MODEL_PATH`
(`add_special_tokens=False`). This needs no extra API surface. Retokenization
can merge or split the last token, so an output one token short is not counted
as a divergence (`RETOKENIZE_SLACK`).

Budget (named constants in `checker.py`):

- `EXACT_PREFIX_TOKENS = 32`: a pin matches if all 32 tokens are identical.
- `MIN_EXACT_FRACTION = 0.9`: at least 90 percent of pins must match.
- `MIN_DIVERGENCE_POSITION = 8`: every other pin's first mismatch must be at
  index 8 or later.
- `MIN_PINS = 8`: a pins file with fewer entries is rejected.

A missing or malformed `reference/pins.json` fails the checker with an error
naming the file. `--skip-pins` bypasses the gate for local development; the
benchmark harness never passes it.

## Generating the pins (once, on the cluster)

```bash
python3 accuracy_checker/make_pins.py --model-path "$MODEL_PATH"
```

It loads the weights with transformers (`device_map="auto"`, bf16), applies the
chat template with thinking off, and greedy-decodes 32 tokens per prompt with
EOS suppressed, one prompt at a time. Prompts are fixed in the script (8 short
plus 8 seeded long ones). If transformers cannot load the served MXFP4
checkpoint, pass a loadable copy of the same model as `--model-path` and the
served checkpoint as `--tokenizer-path`. Commit the resulting file.

## Tests

```bash
uv run pytest examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke/accuracy_checker/test_checker.py -q --no-cov -p no:tach
```
