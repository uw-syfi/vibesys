# Tensor parallelism across NeuronCores

Implements the tensor-parallel axis of [`algorithms/parallelism.md`](../../algorithms/parallelism.md)
with native `torch.distributed` on TorchNeuron. Reach for it when weights plus
KV cache exceed one logical core's ~24 GiB, or when decode is memory-bandwidth
bound and more cores' HBM bandwidth helps.

Prerequisites: [`neuron-pytorch.md`](neuron-pytorch.md), [`hardware.md`](hardware.md)
(logical cores, LNC).

## The model

One process per logical NeuronCore. `torchrun` starts them; the `neuron`
process group assigns each worker a core from `LOCAL_RANK` /
`LOCAL_WORLD_SIZE`. A trn2.3xlarge has 4 logical cores, so TP ∈ {1, 2, 4}.

```python
import torch, torch.distributed as dist
import torch.distributed._functional_collectives as fc
import torch_neuronx

dist.init_process_group(backend="neuron")         # before allocating any neuron tensor
rank, tp = dist.get_rank(), dist.get_world_size()
dev = torch.device("neuron")                       # this rank's core

# Megatron split: column-parallel up/gate/QKV, row-parallel down/O, one all-reduce per block.
w_up = full_up[:, rank * shard:(rank + 1) * shard].contiguous().to(dev)
w_down = full_down[rank * shard:(rank + 1) * shard, :].contiguous().to(dev)

@torch.compile(backend="neuron", fullgraph=True)
def mlp(x):
    y = torch.nn.functional.silu(x @ w_up) @ w_down
    return fc.all_reduce(y, "sum", dist.group.WORLD)   # collective inside the graph
```

```bash
torchrun --standalone --nproc-per-node=4 server.py
NEURON_RT_VISIBLE_CORES=2-3 torchrun --standalone --nproc-per-node=2 server.py   # pin a core range
```

Verified on trn2 (SDK 2.33): a compiled column/row-parallel MLP with a
functional `all_reduce` inside the region matched the CPU reference at TP=2 and
TP=4.

- **Use functional collectives** (`torch.distributed._functional_collectives`)
  inside compiled regions; they trace into the graph. In-place
  `dist.all_reduce(t)` is for eager code.
- **Shard attention by KV head.** Give each rank `kv_heads / tp` KV heads and the
  matching query heads; its KV cache holds only those heads. If `kv_heads < tp`,
  replicate KV heads rather than splitting a head.
- **Shard the LM head by vocab** and gather the logits, or do the argmax per
  shard and reduce `(max, index)` pairs.
- DTensor / `parallelize_module` also work; plain sharded tensors plus
  functional collectives keep the compiled graph simple and predictable.

## Serving shape

Every rank runs the same compiled step on the same inputs. A common layout:

- **Rank 0** owns the HTTP server, tokenizer and scheduler. Each step it
  broadcasts the step's input tensors (token ids, positions, slot mask) with
  `dist.broadcast` from a fixed-shape device buffer, then all ranks run the step.
- **Other ranks** loop: receive inputs, run the step, repeat. They never touch
  the network or tokenizer.
- Sampled tokens come out identical on every rank after the final all-reduce
  or gather; rank 0 reads them back.

Keep the broadcast buffers fixed-shape (padded to the bucket) so the control
plane never introduces a new graph.

## Pitfalls

- **Allocating a neuron tensor before `init_process_group`** binds it to the
  wrong core.
- **Mismatched collective order across ranks** hangs. Every rank must take the
  same branch and the same bucket each step: decide on rank 0 and broadcast the
  decision.
- **A core already owned by another process** (often a stale server) makes
  that rank fail with `Failed to initialize Neuron Runtime`.
- **TP=4 for a model that fits on one core** often loses to 4 data-parallel
  replicas (one process each) for throughput; measure both.

## See also

- [`static-kv-cache.md`](static-kv-cache.md): per-rank cache of local heads.
- [`hardware.md`](hardware.md): per-core HBM.
- AWS docs: [torch.distributed on Neuron](https://awsdocs-neuron.readthedocs-hosted.com/en/latest/torch/guides/torch-distributed.html).
