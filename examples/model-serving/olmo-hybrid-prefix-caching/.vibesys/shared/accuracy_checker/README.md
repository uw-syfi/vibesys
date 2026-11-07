# Olmo-Hybrid accuracy checker

The accuracy gate of both tasks, `from-scratch` and `vllm`. `checker.py` asks the
running server for greedy completions of ten fixed prompts and passes only if every
completion equals the Hugging Face reference stored in `reference_outputs.json`.

## What it checks

For each prompt the checker sends one request to `<url>/v1/completions`:

- `prompt`: the reference prompt's token IDs as `list[int]`, exactly what the
  model's tokenizer produced for the reference;
- `max_tokens`: the prompt's own limit, 5 to 50 tokens;
- `temperature: 0` and `stream: false`, with EOS honored (no `ignore_eos`);
- `model`: `--model`, by default `allenai/Olmo-Hybrid-7B`, the name the benchmark
  also sends.

`choices[0].text` must equal the prompt's reference text exactly. A failed request,
or a response without exactly one choice, fails that prompt. The checker reports every
prompt and exits with status 1 if any failed.

```bash
python checker.py                                    # server on http://localhost:8000
python checker.py --url http://host:8000 --timeout 60
```

It needs only the Python standard library (3.9 or newer). It reaches the server over
HTTP, so it can run next to the server, for example inside a Modal serving container,
without loading a second copy of the 7B model onto the GPU.

## The reference

For each prompt, `reference_outputs.json` holds the prompt token IDs and, for each
reference variant, the output token IDs and their decoded text. The reference is
`transformers` `AutoModelForCausalLM.generate(max_new_tokens=N, do_sample=False)` with
bfloat16 weights and eager attention, decoded with `skip_special_tokens=True`. The
task's earlier checker computed the same reference on the GPU at check time.

Olmo-Hybrid's linear-attention layers run plain-torch kernels, or
flash-linear-attention kernels when that package is installed. On one prompt ("The
capital of France is") the two produce different bfloat16 greedy output, so the file
holds one variant for each kernel choice and the checker accepts either text. On the
other nine prompts the variants agree.

Provenance of the committed file:

| | |
| --- | --- |
| Model | `allenai/Olmo-Hybrid-7B` at revision `4f1cc566f9fdf3ce68da2ab6a788a83d89896dcf`, the revision pinned in each task's `reference/meta.json` |
| GPU | NVIDIA L4, the GPU both tasks target |
| Software | torch 2.15.0+cu132 and transformers 5.22.2; flash-linear-attention 0.6.0 for that variant |

Each variant was generated in its own Modal L4 container by the reference half of the
earlier checker, and `regenerate_reference.py merge` combined the two outputs.

## Regenerating

bfloat16 greedy output depends on the GPU, the kernels and the library versions, so
this reference holds for an L4 with the versions above. Regenerate it when the task's
target GPU, the model revision or the prompts change, on the GPU the task targets
after the change. `generate` needs one CUDA GPU with room for the bfloat16 model,
about 15 GB:

```bash
REV=4f1cc566f9fdf3ce68da2ab6a788a83d89896dcf
uv run --script regenerate_reference.py generate \
    --model allenai/Olmo-Hybrid-7B --revision "$REV" --output torch.json
uv run --with flash-linear-attention==0.6.0 --script regenerate_reference.py generate \
    --model allenai/Olmo-Hybrid-7B --revision "$REV" --output fla.json
python regenerate_reference.py merge torch.json fla.json --output reference_outputs.json
```

`generate` downloads the pinned revision, records the GPU name and the library
versions, and names its variant by whether flash-linear-attention is installed.
`merge` refuses outputs from different model revisions, or outputs whose prompts or
prompt tokens differ. Commit the new file in the same change as the task's
`[catalogue.hardware]` and `reference/meta.json`. The prompts are `PROMPTS` in
`regenerate_reference.py`, and the example's tests check that the committed file holds
exactly those prompts.
