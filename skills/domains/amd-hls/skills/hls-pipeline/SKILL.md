---
name: hls-pipeline
description: 'Guide users on how to apply pipeline pragma in C/C++ regions, analyze region validity for pipelining, and configure II, rewind, and style options'
license: MIT
metadata:
  author: "Sheng Wang"
  version: "1.0.0"
---

> **VibeSys note:** in a VibeSys run the task evaluator runs every Vitis flow (C simulation, synthesis, co-simulation, implementation) and returns its reports as evaluation evidence. Take the top function from the task's candidate contract. The skills this text names for running flows or reading reports (`hls-run-flow`, `hls-synth-report`, `hls-component-basic-info`, `hls-optimize`, `hls-loop-unrollable`) are not installed; use the evaluator's reports and the task contract instead.
<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->
You are an expert guide for applying HLS pipeline pragmas in C/C++ code regions. Your role is to:
1. **Guide users on how to apply pipeline pragmas** in function and loop regions
2. **Analyze whether a region is valid** for pipeline optimization
3. **Explain and configure pipeline options**: II (Initiation Interval), rewind, and style parameters
4. **Provide actionable recommendations** for optimal pipeline configuration

Strictly follow our rules and provide clear, practical guidance.

#### Concepts:
- **Iteration Interval (II)**: the iteration interval cycles for the pipeline
- **Top function**: Retrieved via `~{getActiveComponentTopFunction}`
- **Parallel region**: a region where more than one execution can run in parallel and overlap. In an HLS kernel, it is typically the top function or a dataflow region.
- **"Loop under loop" concept**: A loop B is considered "under" loop A when:
  - **Direct nesting**: Loop B is directly nested inside loop A's body
  - **Indirect through function calls**: Loop A's body contains a function call, and loop B exists inside the callee function or is called by the callee function (at any depth of the call chain)

- **"Loop under function" concept**: A loop B is considered "under" a function A when:
  - **Direct containment**: Loop B is directly inside function A's body
  - **Indirect through function calls**: Function A calls another function, and loop B exists inside that callee function or is called by it (at any depth of the call chain)

#### Scope:
- Pipeline pragma can be defined inside function body region or loop body statement region
- Do NOT check code in test bench files
- Do NOT check code in the `main` function
- Check the code step by step

---

#### How to Apply Pipeline Pragma:

**Basic Syntax:**
```cpp
#pragma HLS pipeline [II=<int>] [style=<stp|flp|frp>] [rewind=true|rewind=false]
```

**Pipeline Types:**
- **Loop Pipeline**: Apply to loop regions (for, range-based for, while, do-while)
- **Function Pipeline**: Apply to function body regions

---

#### Region Validity Analysis Rules:

**1. Function Pipeline Feasibility**
   Function pipeline is valid when ALL of the following conditions are met:
   - **1.1** The function is called more than once in one parallel region.
   - **1.2** All loops under the current pipelined function must be fully unrollable.
     - **Note**: For detailed analysis of whether a loop can be fully unrolled, refer to the `hls-loop-unrollable` skill.
   - **1.3** There must be NO conflicting pragmas under the function:
     - No function dataflow pragma
     - No loop dataflow pragma
     - No loop flatten pragma
     - No partial unroll loop pragma
     - No loop unroll off pragma
     - No loop pipeline pragma

**2. Loop Pipeline Feasibility**
   Loop pipeline is valid when ALL of the following conditions are met:
   - **2.1** All sub-loops under the pipelined loop must be fully unrollable.
     - **Note**: For detailed analysis of whether a loop can be fully unrolled, refer to the `hls-loop-unrollable` skill.
   - **2.2** There must be NO conflicting pragmas under the pipelined loop:
     - No function dataflow pragma
     - No loop dataflow pragma
     - No loop flatten pragma
     - No partial loop unroll pragma
     - No loop unroll off pragma
     - No nested loop pipeline pragma

---

#### How to Configure Pipeline Options:

**Option 1: II (Initiation Interval) Parameter**

The II parameter controls how many cycles between starting consecutive iterations.

**Syntax:**
```cpp
#pragma HLS pipeline II=<positive_integer>
```

**How to Choose II Value:**
The following factors will affect the achieved II:
  - Whether loop-carried dependencies exist or not
  - Whether resources such as IO ports and memory access ports are sufficient for full parallelization
  - Whether timing can be met at the desired clock frequency after Vivado placement and routing, since a smaller II sometimes enlarges the achieved clock period

  Users can start with II=1, and check the target II failure message to resolve the above three factors, then retry. If it fails again, increase the II until reaching an optimal II result for the pipeline.
- **Automatically Choose II**
  - When `II` is not specified in the pipeline pragma, the HLS tool will choose the II with a timing-first principle.


**II and Clock Period Trade-off:**
- If `csynth_design` reports target II cannot be met due to dependencies, consider:
  - Increasing clock period (reducing frequency) to achieve lower II: With a longer clock period, operations that previously required two cycles can now be combined into a single cycle. This reduces the critical path distance (measured in cycles) for loop-carried dependencies, enabling a lower II.
  - Longer clock period allows more logic per cycle, potentially resolving dependencies
  - Balance: Lower II (higher throughput) vs. Lower frequency (longer clock period)

---

**Option 2: Rewind Parameter**

The rewind option reduces delay between consecutive executions of the same loop.

**Syntax:**
```cpp
#pragma HLS pipeline rewind
```

**Recommendation: Prefer auto rewind (do NOT specify the rewind value). If the tool doesn't rewind automatically when you need it, force it with `rewind=true`.**

*Auto rewind* is the default behavior when no explicit rewind value is given. When the `rewind` option does NOT appear in the pipeline pragma, the HLS tool automatically decides whether to rewind the loop based on the surrounding context. This tool-driven decision is safer and more accurate than forcing it manually, so by default omit the `rewind` option entirely and let the tool handle it:
```cpp
#pragma HLS pipeline II=1          // auto rewind — the recommended default
```

**Forcing rewind (only with a specific, verified reason).** When you have a concrete, validated reason to override the tool's automatic decision, you can force the behavior explicitly:
- `rewind=true` — force the loop to be rewound, even if the tool would not have done so automatically:
  ```cpp
  #pragma HLS pipeline II=1 rewind=true
  ```
- `rewind=false` — force the loop NOT to be rewound, overriding the tool's automatic rewind:
  ```cpp
  #pragma HLS pipeline II=1 rewind=false
  ```

Only use `rewind=true` or `rewind=false` when you have verified that the forced behavior is correct for your design. In the common case, users should NOT specify the rewind value at all and should rely on auto rewind.

**Validity Rules:**
- **ONLY valid for loop pipeline** (NOT for function pipeline)
- Creates error if applied to function pipeline

**When Rewind Helps (background — this is what auto rewind detects for you):**

Rewind is beneficial when the pipelined loop runs more than once consecutively. This occurs when ANY of these structural signatures holds:

- **R1 — Loop inside a function called multiple times in one parallel region.** The loop's enclosing function is called more than once in the same parallel region (e.g. two `func()` calls, or a function called inside an outer loop). Each call re-runs the loop, so consecutive executions occur.
- **R2 — Loop nested under an outer repeating loop.** The pipelined loop (directly, or indirectly through function calls) sits under an outer `for`/`while` loop that iterates more than once — including an outer loop carrying `#pragma HLS dataflow`.
- **R3 — Loop under a dataflow region that is itself entered repeatedly.** The loop is part of a dataflow region that re-executes (e.g. the dataflow region is inside a repeating outer loop, or the top function is called repeatedly).

If NONE of R1–R3 hold, the loop runs exactly once per kernel invocation and rewind has no benefit. In all of these cases — whether rewind would help or not — auto rewind makes the right call, so you still simply omit the option.

Worked example:
```cpp
void func1(...) { for (...) {/*loop A*/} }   // loop A runs consecutively (R2)
void func2(...) { for (...) {/*loop B*/} }   // loop B runs consecutively (R2)
void top(...) {
    for (int iter = 0; iter < 2; iter++) {   // outer loop runs twice
#pragma HLS DATAFLOW
        func1(...);  func2(...);             // A and B each run twice consecutively
    }
}
```
Loops A and B both benefit from rewind, but you should still write `#pragma HLS pipeline II=1` (no `rewind`) and let auto rewind apply it — the tool detects the consecutive execution for you.

**Benefits (of rewinding, whether auto or explicit):**
- Improves overall throughput and latency when the loop is executed repeatedly in immediate succession

**Drawbacks (why rewind is not always desirable):**
- Can increase resource usage
- Can degrade both timing and II — extra logic is pushed into the pipeline (which may raise the II), and inter-call dependences can further limit the achievable II
- Can create deadlocks, unless the region is flushing

**Default policy:** When adding a loop pipeline, leave the `rewind` option off so the HLS tool applies auto rewind and decides for itself. Only add `rewind` explicitly when you have a specific reason to force it. Note that rewind is loop-only (it has no effect on a function pipeline).

**Example - Recommended (auto rewind):**
```cpp
void process_frames(data_t input[FRAMES][SIZE], int thresholds[FRAMES]) {
    for (int frame = 0; frame < FRAMES; frame++) {
        #pragma HLS dataflow
        // Inner loop runs FRAMES times consecutively. No 'rewind' option needed —
        // auto rewind lets the tool reduce the delay between executions.
        for (int i = 0; i < SIZE; i++) {
            #pragma HLS pipeline II=1
            output[i] = process(input[frame][i], threshold);
        }
    }
}
```

---

**Option 3: Style Parameter**

The style parameter controls how the pipeline handles stalls, and comes in three styles: `stp`, `flp`, and `frp`.

**Syntax:**
```cpp
#pragma HLS pipeline style=<stp|flp|frp>
```

**The Three Styles:**

**3a. style=stp (Stalled Pipeline)** — the default, non-flushable style.
```cpp
#pragma HLS pipeline II=1 style=stp
```
All stages are controlled by a single global stall signal. When any stage encounters a stall condition (e.g., waiting on a stream read, memory access, or backpressure), the entire pipeline freezes.

**When `stp` is safe inside a dataflow region.** Being non-flushable, `stp` can deadlock a dataflow region — but only when a stage can actually stall at runtime. A stall only arises from a **blocking I/O access** (an `hls::stream` read/write, an `ap_hs`/`ap_fifo` port, or an `m_axi` access that can wait on the bus). If the pipelined region contains **NO blocking I/O access** — all its I/O is to non-blocking interfaces such as local BRAM/`ap_memory`, registers, or `ap_none`/`ap_ack`/`ap_vld` ports — then no stage can stall waiting on data, so `stp` cannot cause a dataflow deadlock and is a valid, preferred choice (lowest resource usage). Only when a blocking I/O access is present inside a dataflow region should you switch to `flp`/`frp` to keep the pipeline flushable.

**3b. style=flp (Flushable Pipeline)** — flushable; drains in-flight data while stalled.
```cpp
#pragma HLS pipeline style=flp
```
When the pipeline is waiting for input data, the remaining stages can continue execution and flush out the data to avoid deadlock in dataflow.

*Example — avoiding deadlock with flp:*
```cpp
void filter(int in[N], hls::stream<int> &out) {
    for (int i = 0; i < N; i++) {
        // flp/frp lets already-computed results flush to 'out' even while the
        // pipeline stalls waiting on the next 'in' read, avoiding deadlock
        // when downstream blocks on the stream in a dataflow region.
        #pragma HLS pipeline II=1 style=flp
        int v = in[i]; // 'in' can be any port whose access may block at runtime,
                       // e.g. an hls::stream read, an ap_fifo / ap_hs interface,
                       // or an m_axi access waiting on the bus. When the data is
                       // not yet available, the read stalls and holds up the pipeline.
        .... // some other code before send data to 'out' stream
        out.write(v * 2);
    }
}
```
With the default stalled pipeline (stp), a stall on the `in` read freezes all stages, so results already produced cannot be written to `out`. If the downstream consumer is blocked waiting on `out`, the dataflow region deadlocks. Using `style=flp` allows the in-flight stages to keep flushing data to `out`, breaking the deadlock.

**3c. style=frp (Free Running Pipeline)** — flushable; never stalls, requires blocking I/O.
```cpp
#pragma HLS pipeline style=frp
```
A free-running pipeline never stalls — it runs continuously on every clock cycle regardless of whether input data is available. If input data is not available, a bubble is generated, and the pipeline continues to run. It is similar to a flushable pipeline and can be used to avoid deadlock. It requires at least one blocking I/O (`hls::stream` or `ap_hs`).

**Prefer `frp` for loops with complicated control flow.** When the loop body contains complicated control flow — a conditional `continue` or a conditional `break` in the loop — the branch conditions typically cannot be simplified or combined. Under `stp`, this control logic fans out to the stall/enable signals of every stage register, creating a long, high-fanout control path with a large timing delay. Choose `style=frp` in this case: removing the global stall simplifies the pipeline control logic and reduces fanout, improving timing. (This requires at least one blocking I/O, `hls::stream` or `ap_hs`.)

*Example — better timing with frp:*
When the loop contains complicated control flow whose branch conditions cannot be simplified or combined, the pipeline control logic fans out to the enable signals of every stage register. This long control path generates a large timing delay. `style=frp` removes the global stall, so the pipeline control logic is simpler and has less fanout, improving timing.
```cpp
void process(hls::stream<int> &in, hls::stream<int> &out) {
    for (int i = 0; i < N; i++) {
        // This loop hits both stp weaknesses at once:
        //   1. Complicated, deeply nested control flow with early break/continue
        //      whose branch conditions cannot be simplified or combined.
        //   2. Blocking port access (stream read/write) inside those branches.
        // Under stp, both feed the single global stall/enable signal, producing a
        // high-fanout control path with a large timing delay.
        // 'frp' removes the global stall, so the control logic is simpler, has
        // less fanout, and meets timing.
        #pragma HLS pipeline II=1 style=frp
        int a = in.read();          // blocking input read
        if (a > THRESHOLD) {
            int x = compute(a);     // result feeds a non-simplifiable branch
            if (x < 0) {
                break;              // early exit on data-dependent condition
            }
            inputOutputAccess(...);
            if (x & 1) {
                continue;           // skip the write on another condition
            }
            out.write(x);           // blocking output write
        }
    }
}
```

**How to Choose Style:**

**Default: let the tool choose.** When `style` is not specified, the HLS tool automatically selects the pipeline style:
- If the pipeline is used with `hls::task`, the **flp** (flushable) style is selected to avoid deadlocks.
- Else if the pipeline control requires high fanout and meets the other free-running requirements (at least one blocking I/O), the **frp** (free-running) style is selected to limit the high fanout.
- Otherwise, the **stp** (stalled) style is selected.

Only specify `style` explicitly when you have a concrete reason to override the tool's automatic choice. Use the table below to pick that override.

| Style | Choose when | Advantage | Disadvantage |
|-------|-------------|-----------|--------------|
| **stp** | No timing issue from high control fanout, and flushing is not needed. Inside a dataflow region, `stp` is only safe when the pipelined region has **NO blocking I/O access** (no `hls::stream`/`ap_hs`/`ap_fifo`/`m_axi` access that can stall); if there is any such access, use `flp`/`frp` instead. Outside a dataflow region, `stp` has no such restriction. | Default. No usage constraints. Typically the lowest resource usage. | Not flushable — can cause deadlocks in a dataflow region when a blocking I/O access stalls, can block already-computed outputs when the next iteration's inputs are missing, and can hit timing issues from high fanout on pipeline controls. |
| **flp** | Flushing is required for better performance or to avoid deadlock (e.g. inside a dataflow region or with `hls::task`). | Flushable — avoids deadlock and can deliver partial results. | Can have a larger II. Greater resource usage due to less sharing when II > 1. |
| **frp** | Better timing is needed (less fanout to register enables from pipeline control) — especially when the loop has complicated control flow (a conditional `continue` or conditional `break` in the loop) — flushing is required, and the loop is one dataflow process with blocking I/O. | Better timing (less fanout), simpler pipeline control logic, flushable. | Moderate resource increase (FIFOs added on outputs). Requires at least one blocking I/O (`hls::stream` or `ap_hs`). Not all pipelining scenarios and I/O types are supported. |

---

**3. Rewind Option Rules (Validation)**
   If the `rewind` option is present:
   - **3.1** The rewind option is ONLY valid for loop pipeline (not function pipeline).
   - **3.2** The rewind option is beneficial when the pipelined loop runs more than once consecutively.
   - **3.3** The rewind option reduces the delay between consecutive loop executions.

**4. Style Option Validation**
   If the `style` parameter is specified, validate:
   - **4.1** Style value must be one of: `stp`, `flp`, or `frp`
   - **4.2** For `style=frp`: Verify at least one blocking I/O interface (hls::stream or ap_hs) exists
   - **4.3** For `style=flp` or `style=stp`: No specific interface requirements

**5. II Parameter Validation**
   If the `II` parameter is specified:
   - **5.1** Validate that the II value is a positive integer
   - **5.2** Note that II specifies the initiation interval cycles between consecutive iterations

**6. Loop Flattening for Multi-Level Nested Loops**
   For multi-level nested loops, by inserting pipeline optimization and loop flattening in the innermost loop, all nested loops can form an integrated unified pipeline, thereby reducing the total execution latency of the entire loop structure. Loop flattening (`#pragma HLS loop_flatten`) combined with pipeline pragma creates a unified pipeline across all loop levels. This optimization is beneficial when the loop nest is flattenable (refer to hls-flattenable skill for detailed analysis rules).


#### Verdict and Guidance:

When analyzing or applying pipeline pragmas:
1. **If region is valid**: Provide guidance on optimal II, style, and rewind configuration
2. **If region is invalid**: Explain which criteria are violated and how to fix them
3. **Always provide actionable recommendations** for pipeline optimization
4. **Explain trade-offs** between different configuration options
