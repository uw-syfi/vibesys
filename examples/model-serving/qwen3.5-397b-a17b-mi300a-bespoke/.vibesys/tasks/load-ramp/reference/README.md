# The numerics gate's reference

`accuracy_checker/checker.py` (`load_reference`) reads and validates these files; a candidate
never sees them change. `accuracy_checker/make_reference.py` generates all three from
transformers, the engine-neutral source.

| File | Holds |
| --- | --- |
| `campaign_continuations.json` | `pins`: 34 prompts (`id`, chat `messages`, the rendered `prompt_ids`) and each one's 32-token greedy `continuation_ids` |
| `campaign_scores.json` | `pins`: the teacher-forced `top1` token and the continuation token's `logprobs` at each of the 32 continuation positions (1,088 in all) |
| `campaign_greedy.json` | `prompts`: for the first 5 prompts, the greedy reply's `text` (special tokens skipped) and `token_ids` (temperature 0, natural EOS, at most 64 tokens, thinking off) |
| `provenance.json` | Written by the generator: model revision, checkpoint fingerprint, dtype, library versions, devices, command, and the three files' SHA-256s |

## Status: still the campaign's files

The three files committed now are not yet the generator's output. They are the optimization
campaign's reference, made by its own from-scratch engine (server "A" of its A/B probe), so
the gate currently rewards matching that engine's numerics. On the 14 prompts they share
with the earlier transformers pins, the two disagree on 5 of 14 32-token continuations.
They are replaced, with `provenance.json`, by one run of the generator (below); the gate's
thresholds stay: at least 1,074 of 1,088 teacher-forced top-1 tokens, and 5 of 5 greedy
replies.

| File | SHA-256 of the campaign's copy |
| --- | --- |
| `campaign_continuations.json` | `767801cfb3251f25f19853b00e7b9363720514452f23bc1c2a33c1398150da4f` |
| `campaign_scores.json` | `f20a0e543b8ff41026c15a899324c82ea2119df3d8acf37da92b2e3f009820e6` |
| `campaign_greedy.json` | `8b6f593ec3b0ddbccd951b88f18958916fa610b2259dc252142b7f89c3996458` |

## How the generator makes the reference

- The prompts are the campaign's (`reference_prompts`: 16 static, 16 generated, and 2
  long-context prompts of about 4,000 and 8,000 words, seed 20260301). A test holds them
  equal to the committed `messages`.
- Model: transformers' `Qwen3_5MoeForCausalLM` text model on the served checkpoint
  `amd/Qwen3.5-397B-A17B-MXFP4`, eager attention, transformers' torch DeltaNet path (the
  generator refuses to run with `fla` or `causal_conv1d` installed, which transformers would
  prefer), transformers' own `generate` with a fresh greedy `GenerationConfig`.
- Precision: every non-expert weight is the checkpoint's bf16, computed in bf16. The routed
  experts stay packed MXFP4 in memory (`PackedExperts`), and each expert a token routes to is
  dequantized when used; fp4 values times a power-of-two scale are exact in bf16, so the
  weights are the stored ones exactly. The module computes what transformers' eager
  `Qwen3_5MoeExperts` computes, in the same order; a CPU test holds its logits bit-equal to
  transformers' on a tiny checkpoint in the real layout. Activations are not quantized:
  the checkpoint's `quantization_config` also declares dynamic MXFP4 activation
  quantization, which the reference leaves out as the engine-dependent part.
- Per prompt: render the chat template with thinking off, greedy-decode 32 tokens with every
  stop token suppressed (`ignore_eos`), then one forward over prompt plus continuation gives
  the top-1 and logprob at each continuation position (log-softmax in fp32).
- `torch.use_deterministic_algorithms` is on. Same inputs, software and hardware give the
  same bytes (a CPU test runs the whole generator twice).

## Running it

One node with four MI300A and the checkpoint, in an image with ROCm torch,
`transformers==5.12.1` (the version of the modeling code in
`.vibesys/tasks/default/reference/`), `safetensors` and `tokenizers`, and without `fla` or
`causal_conv1d`:

```bash
python3 .vibesys/tasks/load-ramp/accuracy_checker/make_reference.py \
  --model-path <checkpoint dir> --model-revision <Hugging Face commit of the checkpoint>
```

It writes the three files and `provenance.json` here and prints the provenance. Commit them
together, and update the task that copies them.

Memory: the packed experts are about 205 GB and the bf16 rest about 20 GB, about 56 GB per
APU with the 60 layers split 15 per device. The same loading scheme (transformers modules,
MXFP4 experts dequantized on use, layers split over the four APUs, bf16) produced the
earlier 16-prompt transformers pins on this hardware. transformers' own `from_pretrained` of
this checkpoint did not finish loading in 45 minutes there.
