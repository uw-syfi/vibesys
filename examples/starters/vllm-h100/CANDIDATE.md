# vLLM H100 Candidate Starter

Input bundles use `workspace.sources` to clone a pinned vLLM source tree into
`./vllm`; this starter provides wrappers that run the local editable source
instead of an installed PyPI package.

Typical local launch:

```bash
python serve.py --model-path /model --served-model-name llama
```

The candidate may edit `vllm/`, `serve.py`, dependency pins, and launch flags.
Evaluator-owned benchmark and accuracy files come from the input bundle and
must not be modified to make a candidate pass.
