You are implementing a causal-LM inference service. The external API/model
contract is fixed; server language and runtime are not.

Preserve named serving engines/source trees (vLLM, SGLang, TensorRT-LLM, or
`third_party/`); optimize it in place and use from-scratch when requested.

For a bespoke objective, define owned layers (attention, MLP, normalization, and
positional encoding); ready-made model or serving-engine implementations are not
allowed. Materialize parameters/buffers on the declared device and verify forward.

{% if interface == "inprocess" %}
Preserve the input-declared import adapter, commonly `VibeServeModel`, with its
specified `from_pretrained(model_dir, device, dtype)` and
`generate(input_ids, max_new_tokens=N)` behavior. Inspect the checker for the
authoritative module path and tensor convention. Production internals may use a
different language/runtime behind this adapter.
{% endif %}

For every scoped text endpoint: do not emit EOS text; stop before a matched stop
string; set `finish_reason` correctly; and count only emitted-text tokens in
`completion_tokens`. Preserve one logical SSE delta per generated model token
even if transport writes are coalesced.

The typed plan names the only endpoint surface in scope. Read the narrow
serving-systems OpenAI API reference when exact request/response/SSE details are
needed, and the FastAPI reference only if the selected architecture uses it.
Do not add unrequested endpoints or preserve FastAPI as an unstated requirement.
