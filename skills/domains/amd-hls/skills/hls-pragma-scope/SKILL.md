---
name: hls-pragma-scope
description: 'Defines HLS pragma scope rules. Must be referenced when analyzing HLS designs or generating any code for HLS designs to ensure pragmas are placed in the correct scope.'
license: MIT
argument-hint: "[<TOP_FUNCTION — top-level HLS function name e.g. 'Kernel'>]"
metadata:
  author: "Yi Gao"
  version: "1.0.0"
---

> **VibeSys note:** in a VibeSys run the task evaluator runs every Vitis flow (C simulation, synthesis, co-simulation, implementation) and returns its reports as evaluation evidence. Take the top function from the task's candidate contract. The skills this text names for running flows or reading reports (`hls-run-flow`, `hls-synth-report`, `hls-component-basic-info`, `hls-optimize`, `hls-loop-unrollable`) are not installed; use the evaluator's reports and the task contract instead.
<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->
You are focused on analyzing whether HLS pragmas are placed in the correct scope.
Strictly follow our rules 1, 2, 3, 4, 5, 6, 7, 8 and do not exert yourself.

#### Concepts:
- **Top function**: the top-level HLS function, get it via argument `<TOP_FUNCTION>` (e.g. `Kernel`), if not provided, call `/hls-component-basic-info` skill to get top_function which is the top-level function name
- **Function scope**: The region inside a function body between `{` and `}`
- **Loop scope**: The region inside a loop body between `{` and `}`
- **Region scope**: Any `{ }` block, including function bodies, loop bodies, and bare (non-function, non-loop) code blocks
- **Variable visibility**: A variable is visible in the scope where it is declared and all nested child scopes
- **Implicit unroll**: When `#pragma HLS PIPELINE` is applied to a loop, all sub-loops within that loop are implicitly fully unrolled

#### Scope:
- Do NOT check whether the pragma options/parameters are correct (that is a separate concern)
- ONLY check whether the pragma is placed in a legal scope
- Check each pragma independently
- Check all pragmas found in code called by or within the **top function**

---

#### Pragma Scope Categories:

**Category A — Function-Body Pragmas**
These pragmas MUST be placed directly inside a function body (not inside a loop or other nested block within the function). They apply to the function that contains them.

| Pragma | Notes |
|--------|-------|
| `#pragma HLS TOP` | Marks a function as the top-level function |
| `#pragma HLS INLINE` | Controls inlining of the enclosing function |
| `#pragma HLS INTERFACE` | Defines interface protocol; see Rule 3 |
| `#pragma HLS DATAFLOW` | Enables task-level pipelining; also valid in loop scope (Category B) |
| `#pragma HLS PIPELINE` | Pipelines the function; also valid in loop scope (Category B) |
| `#pragma HLS FUNCTION_INSTANTIATE` | Creates unique function instances for different constant argument values |
| `#pragma HLS DEPENDENCE` | Specifies dependency information for the function; also valid in loop scope (Category B) |
| `#pragma HLS STABLE` | Marks a function argument as stable |

**Category B — Loop-Body Pragmas**
These pragmas MUST be placed directly inside a loop body. They apply to the loop that contains them.

| Pragma | Notes |
|--------|-------|
| `#pragma HLS PIPELINE` | Pipelines the loop; implicitly unrolls all sub-loops |
| `#pragma HLS UNROLL` | Unrolls the enclosing loop only |
| `#pragma HLS LOOP_FLATTEN` | Flattens from the current loop outward through enclosing loops; `off` excludes the current loop from flattening with both its sub-loops and its parent loops |
| `#pragma HLS LOOP_TRIPCOUNT` | Provides trip count estimate for variable-bound loops |
| `#pragma HLS DATAFLOW` | Enables task-level pipelining within the loop |
| `#pragma HLS DEPENDENCE` | Specifies dependency information for the loop; also valid in function scope (Category A) |

**Category C — Region Pragmas**
These pragmas are placed inside a code region. A region is any `{ }` block — this includes function bodies, loop bodies, and bare (non-function, non-loop) `{ }` blocks. Function and loop scopes are special cases of regions, so all region pragmas, except OCCURRENCE, are also valid directly inside function bodies and loop bodies.

| Pragma | Notes |
|--------|-------|
| `#pragma HLS ALLOCATION` | Limits instance count of functions/operations within the region |
| `#pragma HLS EXPRESSION_BALANCE` | Controls expression tree balancing within the region |
| `#pragma HLS LOOP_MERGE` | Merges consecutive loops within the region |
| `#pragma HLS LATENCY` | Constrains the minimum and/or maximum latency of the region |
| `#pragma HLS OCCURRENCE` | Specifies that the region executes at a lower rate than the enclosing pipeline; MUST be inside a conditional block |

**Category D — Variable Pragmas**
These pragmas reference a specific variable and MUST be placed in a scope where that variable is visible.

| Pragma | Notes |
|--------|-------|
| `#pragma HLS ARRAY_PARTITION` | Partitions an array into smaller arrays or registers |
| `#pragma HLS ARRAY_RESHAPE` | Reshapes an array by combining partitioning with word-width increase |
| `#pragma HLS BIND_STORAGE` | Specifies storage implementation for a variable |
| `#pragma HLS BIND_OP` | Specifies implementation for an operation |
| `#pragma HLS STREAM` | Implements a variable as a FIFO stream; see Rule 5 |
| `#pragma HLS ARRAY_STENCIL` | Enables stencil pattern optimization |
| `#pragma HLS DISAGGREGATE` | Disaggregates a struct into individual elements |
| `#pragma HLS AGGREGATE` | Aggregates struct fields into a single wide word |

---

#### Analysis Rules:

**1. Enclosing Scope Rule**
   A pragma applies to its **enclosing scope** — the nearest `{` `}` block that directly contains it.
   - **1.1** A pragma placed before a function or loop (outside its braces) does NOT apply to that function or loop. It is **misplaced**.
   - **1.2** A pragma placed after the closing brace of a function or loop does NOT apply to that function or loop. It is **misplaced**.
   - **1.3** A pragma must be placed **inside** the `{` `}` of the scope it is intended to affect.

**2. Function-Body Pragma Placement (Category A)**
   - **2.1** Category A pragmas MUST appear directly inside a function body, not nested inside a loop or conditional block within the function.
   - **2.2** Exception: `PIPELINE`, `DATAFLOW`, and `DEPENDENCE` are dual-scope — they are legal in both function bodies and loop bodies (Category A and B).

**3. Interface Pragma Placement**
   - **3.1** `#pragma HLS INTERFACE` is ONLY valid inside the **top function** body.
   - **3.2** Placing `INTERFACE` pragmas in sub-functions is **illegal** — they will be ignored.
   - **3.3** The `port=` argument must reference a parameter of the top function.

**4. Loop-Body Pragma Placement (Category B)**
   - **4.1** Category B pragmas MUST appear inside a loop body.
   - **4.2** `UNROLL` applies to the **immediately enclosing loop** only. Placing it in an outer loop does NOT unroll inner loops.
   - **4.3** `LOOP_FLATTEN` (without `off`) can be placed inside **any loop** in a nest, typically the innermost. It flattens the loop containing it outward through the enclosing loops, stopping at a loop marked `LOOP_FLATTEN off` or at an unflattenable loop boundary.
   - **4.3.1** `#pragma HLS LOOP_FLATTEN off` must also be placed inside a loop body. Call the loop whose body directly contains the pragma **L** — the pragma applies to **L** only. It **isolates L in both directions**: **L** is NOT flattened with the loops nested **inside L**, and **L** is NOT flattened with the loops that **enclose L**.
   - **4.3.2** `off` acts as a boundary only at its own loop — loops nested **deeper** inside it may still be flattened among themselves. Example: in `L1 > L2 > L3`, `LOOP_FLATTEN off` inside L1 plus `LOOP_FLATTEN` inside L3 fuses L2 with L3 while L1 stays a separate loop level.
   - **4.3.3** `LOOP_FLATTEN off` inside the **innermost** loop disables flattening of that loop entirely, including the automatic flattening the tool would apply with no pragma present.
   - **4.4** `LOOP_MERGE` must be placed in the scope that **contains** the consecutive loops to be merged (i.e., the parent function or outer loop body), NOT inside any of the loops being merged.
   - **4.5** `LOOP_TRIPCOUNT` is only meaningful for loops with **variable bounds** and is used for reporting only; it does not affect synthesis.
   - **4.6** `DEPENDENCE` inside a loop body specifies dependencies for that loop. Placing it in the function body specifies dependencies for that function.

**5. Region Pragma Placement (Category C)**
   - **5.1** Category C pragmas are valid inside any `{ }` block — including function bodies, loop bodies, and bare code regions.
   - **5.2** `INLINE region` behaves differently from `INLINE` without the `region` keyword — `INLINE region` inlines all function calls **within** the region, while `INLINE` (Category A) inlines the enclosing function itself at its call sites.
   - **5.3** `OCCURRENCE` MUST be placed inside a conditional block within a pipelined scope. It indicates the block executes at a fraction of the pipeline rate.
   - **5.4** `ALLOCATION` pragmas with `type=function` must be placed in the scope of the caller, not in the function being limited.

**6. Variable Pragma Visibility (Category D)**
   - **6.1** The `variable=` argument must reference a variable that is **visible** (in scope) at the pragma's location.
   - **6.2** A variable pragma placed in a scope where the target variable is not visible is **illegal**.
   - **6.3** For function parameters: variable pragmas referencing parameters must be placed inside that function's body.
   - **6.4** For local variables: variable pragmas must be placed at or after the variable's declaration, within the same scope or a nested scope.
   - **6.5** `#pragma HLS STREAM` cannot be applied to a **top function argument** (use `INTERFACE mode=ap_fifo` or `INTERFACE mode=axis` instead). It is legal on any other visible variable, including sub-function arguments.
   - **6.6** Variable pragmas are **propagated across function calls**. When a variable pragma is applied to a variable in the caller, it propagates to the corresponding parameter in the callee. This means a pragma placed on an argument in the calling function also takes effect inside the called function.

**7. PIPELINE and Implicit Unroll Interaction**
   - **7.1** When `#pragma HLS PIPELINE` is applied to a loop, all loops nested inside that loop are **implicitly fully unrolled**.
   - **7.2** When `#pragma HLS PIPELINE` is applied to a function, all loops inside that function are **implicitly fully unrolled**.
   - **7.3** If an inner loop cannot be unrolled (e.g., variable trip count), the `PIPELINE` on the outer scope will fail. The `PIPELINE` pragma will be reported as ignored in the synthesis log.

**8. DATAFLOW Scope Rules**
   - **8.1** `#pragma HLS DATAFLOW` in a function body enables task-level pipelining between sequential processes (function calls or loops) in that function.
   - **8.2** `#pragma HLS DATAFLOW` in a loop body enables task-level pipelining between sequential processes within **each iteration** of that loop.
   - **8.3** `DATAFLOW` and `PIPELINE` are **mutually exclusive** in the same scope — they cannot both be applied to the same function body or the same loop body.
   - **8.4** Refer to the `hls-dataflow` skill for detailed canonicality rules once scope placement is validated.

---

#### Verdict:
If **all** pragma placements follow the scope rules above, the pragmas are **correctly scoped**. Otherwise, list each violation with the rule number, the offending pragma, and the correction needed.
