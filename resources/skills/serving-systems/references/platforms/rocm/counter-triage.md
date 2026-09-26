# Counter triage

Turns a roofline point plus a PMC counter capture into **one verdict**, and
the verdict into a serving-level next step. This is the step between "the
kernel is slow" and "here is what changes."

## Prerequisites

A roofline point from [`roofline.md`](roofline.md) and, if the point sits
under both roofs, a counter capture from `counters.py`
(see [`profiler.md`](profiler.md)). If you have no measurement yet, start
there: classifying from source reading is guesswork.

## The decision flow

```
            ┌─ near the COMPUTE roof? ──── yes → COMPUTE-BOUND
roofline ───┤
   point    ├─ near the BANDWIDTH roof? ── yes → BANDWIDTH-BOUND
            │
            └─ far from both roofs → check occupancy
                         │
                         ├─ few waves/CU (VGPR / LDS / workgroup cap) → OCCUPANCY-LIMITED
                         └─ occupancy fine, high stall % → LATENCY-BOUND
```

**"Far from both roofs" is the most common real answer.** Occupancy-limited
and latency-bound both live there, and only waves/CU plus stall percentage
separate them: don't collapse them into a single "not fast enough" bucket.

## The four verdicts and the serving-level action

This skill covers *consuming* kernel libraries, not authoring kernels: see
[`aiter.md`](aiter.md)'s "Out of scope" note. Each verdict below routes to a
serving-level lever first; hand off to `agent-gpu-skills` only once that
lever is exhausted and the remaining gap is inside a specific kernel's
implementation.

| Verdict | Counter signature (rocprof-compute Speed-of-Light) | Serving-level lever |
|:--|:--|:--|
| **Compute-bound** | MFMA-busy engine metric high, near the compute roof | Confirm AITER/CK (not a Triton/SDPA fallback) is engaged: [`aiter-engagement.md`](aiter-engagement.md). If it is, and the point still sits well under the roof, the library kernel itself is the ceiling: kernel-authoring territory, out of scope here. |
| **Bandwidth-bound** | high TCC (L2) miss + HBM read-request counters, low MFMA-busy | Expected for skinny decode GEMV and paged-KV attention reads. Check KV layout, quantization, and batch size (see [`floor.md`](floor.md)); confirm the kernel isn't a fallback that also adds redundant memory traffic; see [`aiter-engagement.md`](aiter-engagement.md). |
| **Occupancy-limited** | few resident waves/CU; high VGPR or LDS usage per wave; small workgroup count | Usually a kernel-authoring fix (register/LDS budget, grid sizing), out of scope here; hand off to `agent-gpu-skills`. At the serving level, check whether batch size/concurrency is structurally too small to fill the grid. |
| **Latency-bound** | occupancy is fine, high issue/stall counters, low IPC | If the trace shows many small kernels with large gaps between them, this usually isn't a kernel problem at all: it's host/launch overhead. See [`algorithms/async-scheduling.md`](../../algorithms/async-scheduling.md) and [`floor.md`](floor.md)'s HIP graphs section before touching kernel internals. |

## Compute-bound evidence: express MFMA utilization as a fraction of peak

A raw MFMA instruction rate (instructions/cycle) has no reference point: the
reader can't tell how close that is to the ceiling. `counters.py triage`
reports MFMA utilization as a **fraction of peak** instead, in priority order:

1. **Measured**: `SQ_VALU_MFMA_BUSY_CYCLES / (GRBM_GUI_ACTIVE * SIMD_NUM)`.
   Mirrors rocprof-compute's own `MfmaUtil` derived metric exactly (confirmed
   in a real MI210 `rocprofv3 --list-avail` dump). `SIMD_NUM` comes from a
   captured `*_agent_info.csv`'s `Cu_Count * Simd_Per_Cu` when present,
   otherwise the static per-arch table (MI210: 104 CUs x 4 SIMD/CU = 416).
2. **`--flops` fallback**: when `SQ_VALU_MFMA_BUSY_CYCLES` wasn't captured
   but the kernel's FLOP count is known (e.g. `2*M*N*K` for a GEMM of known
   shape), pass it via `--flops`: achieved FLOP/s (FLOPs / elapsed time, from
   the PMC rows' own timestamps) divided by the arch's spec dense bf16/fp16
   TFLOP/s peak. Only fires for a kernel that actually issued MFMA
   instructions, so it can't mislabel a non-MFMA kernel.
3. **Raw rate fallback**: if neither is available, the evidence falls back to
   the old `insts/cycle` number, explicitly labeled "no peak reference" so
   it's never mistaken for a peak-normalized signal.

The fraction is clamped to `[0, 1]` with a stderr warning when a raw ratio
falls outside it (inconsistent/stitched counters, or a `--flops` hint that
doesn't match the kernel that ran): treat that warning as a reason to
distrust the inputs, not the GPU. Compute-bound triggers at 30% of spec peak,
well below the ~45-55% of spec peak a tuned GEMM realistically reaches; spec
peak itself is not the achievable ceiling.

## Cross-check: kernel-library classification

Before trusting either roof position, confirm which library actually ran.
`analyze_rocprof.py families` classifies dispatched kernels into AITER / CK /
hipBLASLt / Triton / torch-native buckets directly from the trace: a
compute-bound verdict against a Triton fallback and a compute-bound verdict
against a tuned AITER kernel mean different things (library-selection problem
vs. genuine hardware ceiling). Run this check first; it's cheaper than a full
counter capture and it can invalidate the counter capture's interpretation
entirely.

## How to drive it

1. Place the point: `compute.py --roof-only`-equivalent, or `torch_profiler
   roofline` (see [`roofline.md`](roofline.md)).
2. If far from both roofs → full `compute.py profile` + `analyze`, read the
   Speed-of-Light and memory-chart blocks.
3. Cross-check with `analyze_rocprof.py families` that the profiled kernel is
   the one you intend, not a fallback.
4. Apply **one** serving-level lever from the table above.
5. Re-profile and A/B per [`measurement-protocol.md`](measurement-protocol.md).

One lever at a time. Two simultaneous changes make the delta unattributable,
and if they interact you cannot even tell the sign of each.

## Verify the verdict was right

After the fix, **the roofline point should move toward a roof** and the
targeted counter should move in the predicted direction. Wall time alone is
not enough:

| Observation | Meaning |
|:--|:--|
| Point moved toward a roof, counter moved as predicted | verdict was right, lever worked |
| Wall time down, point and counters unchanged | work moved elsewhere; re-triage |
| Nothing moved | wrong verdict, return to the decision flow |

## Failure modes

| Symptom | Cause | Fix |
|:--|:--|:--|
| "It's slow so it's compute-bound" | slow ≠ compute-bound | place it on the roofline first |
| Occupancy vs. latency confused | both sit far from the roofs | only waves/CU and stall % separate them |
| Optimized a kernel worth 2% of runtime | Amdahl | pick targets from `analyze_rocprof.py kernels`' longest rows |
| Verdict flips between runs | measurement noise | [`measurement-protocol.md`](measurement-protocol.md): warm, ≥3 reps, locked clocks |
| Bandwidth-bound verdict, HBM counter low | working set is cache-resident, not HBM-bound | check hit rate; this is a cache-locality question, not an HBM-bandwidth one |
| Compute-bound verdict against a fallback kernel | never checked which library ran | [`aiter-engagement.md`](aiter-engagement.md) |

## See also

- [`roofline.md`](roofline.md): building the point this triage starts from
- [`profiler.md`](profiler.md): `counters.py` (plan/report/triage) and `compute.py` (doctor/profile/analyze)
- [`aiter-engagement.md`](aiter-engagement.md): confirming which kernel library actually ran
- [`measurement-protocol.md`](measurement-protocol.md): the A/B discipline this triage feeds into
