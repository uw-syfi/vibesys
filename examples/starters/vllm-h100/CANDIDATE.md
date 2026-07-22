# vLLM H100 Candidate Starter

Input bundles use `workspace.sources` to clone a pinned vLLM source tree into
`./vllm`; this starter provides a local bridge that forwards default evaluator
traffic to the candidate's Modal H100 app.

Typical local launch:

```bash
python serve.py
```

The bridge starts `modal serve main.py`, discovers the generated `*.modal.run`
URL, and listens on `http://localhost:8000` for the evaluator-owned benchmark
and accuracy checker. Set `MODAL_BACKEND_URL` to proxy to an existing Modal web
endpoint instead.

The candidate may edit `vllm/`, `main.py`, `serve.py`, dependency pins, and
launch flags.
Evaluator-owned benchmark and accuracy files come from the input bundle and
must not be modified to make a candidate pass.
