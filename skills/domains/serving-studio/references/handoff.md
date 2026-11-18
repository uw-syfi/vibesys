# A serving handoff

What goes in a handoff (the handoff reference of `vibesys:optimize`: where it lives, versions,
the loop) when the system is an LLM serving stack and the model is ServingStudio. Modeled on the
ServingStudio team's own sim-to-real handoffs: a frozen design, a contract, and a ladder of items
from the smallest part that runs to the whole system under the benchmark.

## README.md

- **Frozen at:** the endpoint or checkout and the simulator revision every number came from, the
  deployment kind (match or proxy, each difference named) and the head residual when there is a
  measured head. When the simulation reports another metric than the task's, or runs on other
  hardware, no residual can be computed: write "Head residual: none" with the reason, and rest
  the next version on measurements and profiles of the head.
- **Design in one sentence:** model, hardware and layout, the workload, and the result the
  simulator gives for it.
- **Design:** the model and precision; the GPU layout (parallelism, replicas, a prefill/decode
  split) and why it won among the deployments evaluated; the kernel at each position (attention,
  GEMMs, MoE, sampling) and its library; the scheduler (batching, chunk size, admission,
  ordering); the KV cache (block size, capacity in tokens, prefix reuse). Say why each choice,
  with the simulated comparison that made it.
- **Reference numbers:** the whole system's simulated metric on the benchmark's workload, and
  where the model is weak (`Untrusted:` of [prediction.md](prediction.md)).

## CONTRACT.md

What a measurement must match to be compared with a reference: the checkpoint, precision, GPUs
and their count, the workload exactly as the benchmark sends it (copied from `OBJECTIVE.md` and
the benchmark, not paraphrased), the metric's definition, and the correctness check. It points
at `vibesys:measure` for how to measure; it adds no benchmark of its own. Anything a measurement
cannot match goes in `STATUS.md` as a setup difference before any comparison.

## ITEMS.md: the ladder

Order items so each one runs and is measured before the next builds on it. A ladder for a stack
built from nothing, to adapt and cut:

1. **Skeleton:** the server answers the benchmark's API with correct output, at any speed. This is
   the bootstrap of a base that fails its check.
2. **Kernels:** the kernels the design names, each against its simulated time at the benchmark's
   shapes (kernel rows of the simulator's reports), one item per kernel that carries a large
   share.
3. **One iteration:** a forward pass of a batch the scheduler would form, against the simulated
   iteration time.
4. **Scheduler:** batching and admission as designed, measured on the benchmark's workload.
5. **Cache and layout:** prefix reuse, KV capacity, and the GPU layout (replicas, a split), each
   against the simulated whole-system metric of the design with and without it.

For a task that tunes a stack that exists, the ladder is shorter: the items are the changes the
simulator ranks first, and an item for one kernel names the kernel and its simulated share at
the head, without predicting the gain.

Give each reference number its source: the simulation's result directory or id, and the
configuration it ran. A number from a proxy deployment is a relative change, never an absolute
target ([prediction.md](prediction.md)).

## When the simulator lacks the model

Step 1 of the skill finds no deployment (another architecture, a precision the catalog does not
hold), or a model gap blocks a choice: a model-extension item. Its worker clones
ServingStudioSim at the pinned commit into its own directory (never `$SERVINGSTUDIO_SIM`), and
builds it as [setup.md](setup.md) says. Then it adds the missing kernel rows by profiling them on
the task's hardware (GPU work goes through `vibesys-cli submit command`) or the missing model
code, checks the extension against a small measured case (one request's per-layer times), and
hands back the commit and that check's residual. ServingStudioSim's own documentation says how to
add a model or kernel rows. Until a version points at the extended checkout, plan without the
simulator for that part.
