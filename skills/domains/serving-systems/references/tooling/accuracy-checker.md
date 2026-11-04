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
