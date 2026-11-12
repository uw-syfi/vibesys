# The numerics gate's reference

The three files are the reference the optimization campaign on this example accepted every
change by, copied unchanged. `accuracy_checker/checker.py` (`load_reference`) reads and
validates them; a candidate never sees them change.

| File | Holds | SHA-256 |
| --- | --- | --- |
| `campaign_continuations.json` | `pins`: 34 prompts (`id`, chat `messages`, the rendered `prompt_ids`) and each one's 32-token greedy `continuation_ids` | `767801cfb3251f25f19853b00e7b9363720514452f23bc1c2a33c1398150da4f` |
| `campaign_scores.json` | `pins`: the reference's teacher-forced `/v1/score` rows (`top1` token ids and their `logprobs`) at each of the 32 continuation positions | `f20a0e543b8ff41026c15a899324c82ea2119df3d8acf37da92b2e3f009820e6` |
| `campaign_greedy.json` | `prompts`: 5 short chat prompts and the reference's greedy reply `text` and `token_ids` (temperature 0, natural EOS, at most 64 tokens, thinking off) | `8b6f593ec3b0ddbccd951b88f18958916fa610b2259dc252142b7f89c3996458` |

## Where they come from

The reference is not a transformers forward: the full-precision transformers load of the
397B checkpoint never finished on the cluster. It is server "A" of the campaign's A/B
probe: the campaign's fourth-round build of the from-scratch engine, run in the
configuration that build named as its own reference. The 34 prompts are the static,
generated and long-context prompts of the campaign's `make_pins.py` (seed 20260301; the
default task's copy has the first two kinds);
server A generated each continuation greedily with `ignore_eos`, and then teacher-forced it
through its own `/v1/score` with `top_k` 1 (`top1` and `logprobs` per position).

The thresholds come from the same campaign: at least 1,074 of the 1,088 positions (98.7
percent) must agree on the top-1 token, and every greedy reply's text must match exactly.
