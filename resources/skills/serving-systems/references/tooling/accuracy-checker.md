# Accuracy Checker

Verify that a custom inference implementation produces identical output to HuggingFace `model.generate()`.

## Workflow

1. Load model and tokenizer once (shared between both paths)
2. Build test suite: raw completion prompts + chat-templated prompts
3. For each test case, run both implementations with greedy decoding
4. Compare raw token ID lists and report pass/fail with diagnostics

## Comparison Methodology

### Use greedy decoding for determinism

Both paths must use `temperature=0` / `do_sample=False`. This makes outputs fully deterministic so any difference is a real bug.

Reference path:
```python
output_ids = model.generate(**inputs, max_new_tokens=N, do_sample=False)
```

Custom path: argmax on logits at each step:
```python
next_token = outputs.logits[:, -1, :].argmax(dim=-1, keepdim=True)
```

### Compare token IDs, not strings

Compare the raw `list[int]` of generated token IDs. Decoded strings can mask differences (e.g. whitespace tokens that decode identically).

### Report first divergence point on mismatch

On failure, report:
- Index of first differing token
- Token IDs around the divergence (both paths)
- Decoded text from both paths

```python
for i in range(min(len(ref_ids), len(manual_ids))):
    if ref_ids[i] != manual_ids[i]:
        # report divergence at index i
        break
```

## EOS Handling

The most common source of off-by-one mismatches.

`model.generate()` includes the EOS token in its output. The manual loop must also append EOS to the output list when encountered, before breaking:

```python
token_id = next_token.item()
if token_id == eos_id:
    new_token_ids.append(token_id)  # include EOS to match model.generate()
    break
new_token_ids.append(token_id)
```

## Test Sample Design


Key categories:
- **Short factual** — baseline correctness
- **Long prompts** — stress positional encoding / RoPE
- **Code** — exercises unusual token sequences
- **Chat-templated** — verifies template expansion + special tokens
- **Edge cases** — single token, JSON, numbers (tokenizer boundaries)

Aim for 10-15 test cases covering all categories. Each case: `(prompt, max_new_tokens, description)`.


---

## Test Samples

Each category exercises a different aspect of the generation pipeline. Include at least one sample from each.

## 1. Short factual completion

```python
("The capital of France is", 15, "short factual completion")
```

**Why**: Baseline correctness check. Short prompt, predictable output. Catches fundamental mismatches in logit computation or argmax.

## 2. Story continuation

```python
("Once upon a time, in a land far away,", 50, "story continuation")
```

**Why**: Longer generation (50 tokens) tests that KV cache accumulates correctly over many steps without drift.

## 3. Code completion

```python
("def fibonacci(n):\n    \"\"\"Return the n-th Fibonacci number.\"\"\"\n", 40, "code completion")
```

**Why**: Code tokens have unusual distributions (indentation, operators, keywords). Tests tokenizer edge cases and multi-byte token handling.

## 4. Arithmetic

```python
("1 + 1 =", 5, "arithmetic")
```

**Why**: Very short generation with digit tokens. Catches issues with token ID mappings for numeric tokens.

## 5. Pattern continuation

```python
("A B C D E F G H I J K L M N O P Q R S T U V W X Y Z A B C D E F G", 20, "alphabet pattern")
```

**Why**: Repetitive pattern with many single-character tokens. Tests that attention correctly attends to the full sequence.

## 6. Long prompt

```python
(
    "The following is a detailed explanation of how neural networks work. "
    "Neural networks are computing systems inspired by biological neural networks. "
    "They consist of layers of interconnected nodes or neurons. "
    "Each connection has a weight that adjusts as learning proceeds. "
    "The network processes information using a connectionist approach. "
    "In summary, the key takeaway is that",
    30,
    "long prompt completion",
)
```

**Why**: Long input stresses positional encoding (RoPE). The first-step forward pass processes many tokens at once vs one-at-a-time in subsequent steps — catches KV cache initialization bugs.

## 7. Q&A format

```python
("Question: What is the speed of light?\nAnswer:", 20, "Q&A format")
```

**Why**: Newline characters in prompt exercise multi-line tokenization. The structured format tests whether attention patterns work correctly across line boundaries.

## 8. Number continuation

```python
("The year 2024 was followed by the year", 10, "number continuation")
```

**Why**: Numbers are tokenized in different ways across tokenizers (single digits, multi-digit chunks). Tests that numeric token boundaries are handled identically.

## 9. Single word prompt

```python
("Hello", 15, "single word prompt")
```

**Why**: Minimal context. The model must generate coherently from almost no input. Tests the edge case of a very short prompt (possibly 1-2 tokens including BOS).

## 10. JSON completion

```python
('{"name": "Alice", "age":', 10, "JSON completion")
```

**Why**: Punctuation-heavy input with quotes, colons, braces. These are often multi-character tokens that exercise unusual tokenizer splits.

## 11-14. Chat-templated prompts

```python
([{"role": "user", "content": "What is 2+2? Answer in one word."}], 10, "simple math chat")
([{"role": "user", "content": "Write a haiku about programming."}], 40, "creative chat")
([{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, {"role": "user", "content": "..."}], 40, "multi-turn chat")
([{"role": "user", "content": "Translate to French: 'Good morning, how are you?'"}], 25, "translation chat")
```

**Why**: Chat templates inject special tokens (`<|begin_of_text|>`, `<|start_header_id|>`, etc.) that are model-specific. These tests verify that:
- Template expansion produces the same token sequence in both paths
- Special token IDs are handled correctly by the KV cache
- Multi-turn context (system + user + assistant + user) doesn't cause position mismatch

---

## Evaluating task accuracy on a thinking chat model

The sections above check whether one implementation matches another
token-for-token. A different, equally common question is whether a served
model clears a published task-accuracy bar (e.g. GSM8K exact-match). That
question needs its own protocol, because the failure modes are different:
the model being wrong is indistinguishable from the harness asking it the
wrong way unless the evaluator itself is validated first.

Procedure:

1. Use the chat endpoint with the model's own chat template, not a raw
   completion prompt, when the model is a thinking/reasoning chat model.
2. Decide the thinking mode explicitly with the request-level switch
   (e.g. `enable_thinking`); do not rely on a default.
3. Budget enough output tokens for a full reasoning chain (thousands, not
   hundreds); a truncated chain of thought corrupts the extracted answer,
   not just the token count.
4. Extract the final answer with an extractor robust to reasoning text
   around it (e.g. the number after a `####` marker if present, else the
   last number in the text), not one that assumes the answer is the only
   content.
5. Save every prompt and every raw response. A bad score with no saved
   output cannot be diagnosed after the fact.
6. Validate the evaluator on a small subsample (about 20 questions)
   against the model's own published score before running the full set.
   A harness that clears the bar at n=20 is worth running at scale; one
   that doesn't needs fixing first, not a bigger sample.

### Pitfalls

```
Symptom: an exact-match accuracy evaluation against a thinking chat model
         scores far below its published number (here 20-33 percent
         measured, on a model that scores about 94-97 percent when
         evaluated correctly); responses come back truncated
         mid-calculation or empty.
Cause:   a raw few-shot completion prompt sent to a thinking chat model
         (no chat template) opens an unbounded reasoning continuation
         instead of terminating after one answer. A fixed token budget
         (e.g. 512) cuts that continuation off mid-calculation before the
         model reaches its answer, so an extractor pulls a stray
         intermediate number instead of the final one; or the completion
         API's own default stop strings (e.g. `Question`, `Assistant:`,
         `<|separator|>`) happen to match inside the reasoning text and
         truncate the response to nothing.
Fix:     evaluate through the chat endpoint with the model's chat
         template; set the thinking-mode switch explicitly; raise the
         token budget to fit a full reasoning chain; use a robust
         final-answer extractor; save every prompt and raw response so a
         bad score can be diagnosed instead of re-run blind. See the
         procedure above.
Scope:   any thinking chat model evaluated through a completion-style,
         no-chat-template harness; backend-independent.
Status:  verified (reproduced: the corrected protocol recovered a
         published-range score on the same checkpoint, after the failure
         mode was isolated on a small diagnostic subsample). Stamp:
         sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.
```

Corrected protocol and numbers (Qwen3.5-397B-A17B-MXFP4, GSM8K, 500-question
fixed subset, 8-shot, greedy, `/v1/chat/completions`, thinking mode on,
4096-token budget, concurrency 64, robust extractor): two passes against
one held server scored 94.0 percent and 94.2 percent, pooled 94.1 percent
(941/1000), pooled binomial SE 0.75 points, pass-to-pass gap 0.2 points.
See [`../models/qwen3-5.md`](../models/qwen3-5.md) for the model this was
measured on.

### Nondeterminism is intrinsic, not a batch-composition artifact

Greedy decoding on this stack is not run-to-run text-identical, even at
concurrency 1. Two measurements corroborate this:

- Rerunning the same 100 GSM8K questions twice at the same concurrency
  (64) gave a 22 percent text-identical rate (22/100); rerunning the same
  100 questions at a different concurrency (64 vs. 1) gave only 4 percent
  (4/100), a bigger gap than same-concurrency reruns show. Accuracy itself
  stayed within a few points across all three passes despite the low text
  agreement.
- On a fixed 256-token-per-turn multi-turn workload (144 requests),
  self-agreement measured at the workload's own concurrency (48 sessions)
  was 6.9 percent text-identical, 22.5 percent mean token-agreement
  fraction (shared prefix over reference length), and 0.018 mean absolute
  logprob difference over the shared prefix; measured at concurrency 1 it
  was 2.8 percent text-identical, 20.2 percent token-agreement, and 0.017
  mean absolute logprob difference, not higher than at concurrency 48.
  About 17 to 18 percent of the points where two runs' token streams
  diverged had a top-2 logprob gap under 0.05 (a near-tie), at either
  concurrency.

Concurrency-1 self-agreement is not higher than concurrency-48
self-agreement measured the same way. This rules out cross-request batch
composition as the sole mechanism: if disagreement came only from other
concurrent requests perturbing floating-point reduction order in shared
batched GEMMs, running fully sequentially (no other request ever
in-flight) should have restored near-total agreement, and it did not. The
more likely source is intrinsic per-request numerics (e.g. reduction
order inside a fused GEMM/MoE kernel, or a speculative-decoding
draft/verify path), present per request independent of what else is
running concurrently.

Consequences:
- Text-identity is a weak agreement metric on a stack like this: treat it
  as a coarse floor, not the primary signal.
- Task accuracy with a stated standard error is the right correctness
  gate; see the acceptance tolerance template below.
- A "bit-exact kernel change" claim is a claim about one kernel's output
  against a reference (e.g. a microbenchmark), not about end-to-end
  generations: end-to-end greedy output is expected to differ run to run
  even when every kernel involved is individually bit-exact, because the
  nondeterminism is intrinsic per-request numerics, not a property of
  batch composition.

Scope: serving stacks with intrinsic per-request floating-point
nondeterminism (e.g. reduction-order variation in fused GEMM/MoE kernels,
or a speculative-decoding draft/verify path); measured on
sglang-v0.5.18-rocm700-mi30x with Qwen3.5-397B-A17B-MXFP4 under NEXTN k=3
speculative decoding. Status: verified (measured at two concurrencies on
two independent workloads, both showing the same pattern). Stamp:
sglang-v0.5.18-rocm700-mi30x, 2026-09-13, job-verified.

### Acceptance tolerance template for a lossy serving change

When a candidate change may alter computed numbers (not just kernel
choice or scheduling), gate it on:

1. Task accuracy, measured with the exact same protocol and concurrency
   as the baseline (same dataset, shot count, endpoint, thinking-mode
   setting, and token budget): accept if the candidate's pooled accuracy
   falls within 3 pooled binomial standard errors of the baseline's
   pooled mean. Do not compare a candidate run at a different concurrency
   against a baseline band measured at another concurrency; concurrency
   itself can shift which attractor a run lands in (see above), so the
   comparison must hold concurrency fixed too.
2. The serving harness's own correctness gates, unchanged, on every rep.
3. Agreement metrics (text-identical fraction, mean token-agreement
   fraction, mean absolute logprob difference over the shared prefix) as
   a secondary signal only, with thresholds derived from the baseline's
   own measured self-agreement, not a textbook default: self-agreement on
   a stack with intrinsic nondeterminism is itself noisy across
   measurement occasions (see above), so a candidate-vs-reference
   agreement figure only needs to clear a floor near what two honest
   reruns of the same baseline already show, not near 1.0.

Worked example (Qwen3.5-397B-A17B-MXFP4, GSM8K 500-question baseline,
concurrency 64, pooled accuracy 94.1 percent, pooled binomial SE 0.75
points, level-2 self-agreement measured at the workload's 48-session
concurrency):

| Gate | Threshold |
|:--|:--|
| Task accuracy (baseline +/- 3x pooled SE) | 94.1 +/- 2.24 points, i.e. [91.9%, 96.3%] |
| Level-2 text-identical fraction | >= 0.02 |
| Level-2 mean token-agreement fraction | >= 0.15 |
| Level-2 mean absolute logprob difference (shared prefix) | <= 0.04 |
| Harness correctness gates | 13/13 |

Status: verified (baseline measured, tolerance derived from the same
measurement). Stamp: sglang-v0.5.18-rocm700-mi30x, 2026-09-13,
job-verified.
