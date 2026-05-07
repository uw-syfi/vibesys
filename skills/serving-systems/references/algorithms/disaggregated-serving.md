# Disaggregated serving

Prefill is compute-bound, decode is memory-bandwidth-bound. Running them on the same GPUs wastes one or the other. Disaggregation splits them into independently scaled worker pools connected by a KV transfer path.

## Architecture

```
requests ──► router ──► prefill workers (compute-bound)
                            │
                            └── KV cache ──(transport)──► decode workers (mem-bw-bound)
                                                              │
                                                              └── stream tokens back
```

- **Router** holds the conversation, picks a prefill worker, waits until KV is available on a decode worker, then streams.
- **Prefill worker**: runs prompt forward, emits KV to transport, sends first-token logit back.
- **Decode worker**: receives KV, runs autoregressive decode.
- **Transport**: the crux. See below.

## Why it helps

| Stage | Bottleneck | Good hardware |
|:------|:-----------|:--------------|
| Prefill | compute | high-FLOP (H100/H200/B200) |
| Decode | memory bandwidth + capacity | high-HBM (H200/MI300X) |

With disaggregation you can pick different hardware per stage, or different parallelism (e.g. larger TP for prefill to hit high FLOP). Empirically 3–5× throughput gains vs. co-located serving at the same latency SLOs, for appropriate workloads (long prompts, bursty decode).

## Transports

| Transport | Mechanism | Where |
|:----------|:----------|:------|
| **NIXL** | NVIDIA Inference Xfer Library (RDMA, NVLink P2P, CPU) | vLLM, SGLang, TRT-LLM |
| **Mooncake** | KV store + RDMA | SGLang |
| **Mori** | RDMA-backed EP / KV | SGLang |
| **Native (NCCL)** | in-group NCCL send/recv | TRT-LLM native, vLLM |
| **Ascend / MUSA** | vendor-native | SGLang adapters |
| **Fake / loopback** | for testing | SGLang fake |

Choice depends on: network topology (NVLink domain vs. NIC), partial-transfer support (layer-by-layer streaming), and whether the cluster has an RDMA fabric.

## Streaming vs. blocking transfer

- **Blocking**: prefill finishes entire KV, then transfer, then decode starts. Simple; TTFT on decode worker = prefill + transfer + one decode step.
- **Layer-by-layer streaming**: each prefill layer's KV ships as soon as it's produced; decode can start once first-layer KV arrives (with pipelining). Lower TTFT, higher implementation complexity.

Modern engines (SGLang, TRT-LLM) implement streaming behind the transceiver abstraction.

## P/D ratio

The number of prefill workers per decode worker depends on:

- **Prompt length distribution**: longer prompts → more prefill demand
- **Output length distribution**: longer outputs → more decode demand
- **Model** (MoE shifts work toward decode-side capacity)
- **Transport latency**: slow transport discourages many small prefills

Start with 1:1 and adjust based on queue depth and GPU utilization per pool.

## Compatibility

| Implementation | Engines | Transports |
|:---------------|:--------|:-----------|
| vLLM KV connector | vLLM | NIXL, others via `kv_connector/` |
| SGLang disaggregation | SGLang | NIXL, Mooncake, Mori, native, Ascend, fake |
| TRT-LLM disaggregation | TRT-LLM | native (NCCL), NIXL |

## Engine pointers

| Engine | Core path |
|:-------|:----------|
| vLLM | `vllm/distributed/kv_transfer/`, `kv_transfer_state.py`, connector implementations in `kv_connector/` |
| SGLang | `python/sglang/srt/disaggregation/{encode_server,encode_receiver,encode_grpc_server,decode,prefill}.py`, subdirs `base/`, `common/`, `nixl/`, `mooncake/`, `mori/`, `ascend/`, `fake/` |
| TRT-LLM | `tensorrt_llm/_torch/disaggregation/{transceiver.py,base/,native/,nixl/,resource/}` |

## Pitfalls

- **KV layout must match between prefill and decode workers.** If prefill runs TP=8 and decode runs TP=4, the KV shards don't line up without a re-shard step.
- **Router is a scheduling system, not a proxy.** Routing wrongly fills one side's queue; it needs visibility into both pools.
- **Transport failure mid-transfer.** The request must fail cleanly; partial KV at the decode worker can cause silent corruption if the decode starts anyway.
- **Radix / prefix caching across pools.** A prefix cached on the prefill side isn't visible to the decode side; cache keys must be pool-scoped or the router must stick identical prefixes to the same prefill worker.
- **Quantization at the boundary.** Transferring KV in FP8 halves transport but requires both sides to agree on scheme and dtype.
- **First-token latency accounting.** TTFT spans prefill + transport + (optional) wait-for-slot on decode; don't attribute transport time to the model.

## See also

- `algorithms/parallelism/` — can combine with TP/PP/EP within each pool
- `algorithms/paged-attention/` — KV layout + transfer chunking
- `hardware/nvidia/` — NVLink / NVSwitch / NVL72 / NIC choices
- `engines/sglang/`, `engines/vllm/`, `engines/trtllm/` — source
