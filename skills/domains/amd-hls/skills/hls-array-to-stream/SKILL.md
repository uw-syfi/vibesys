---
name: hls-array-to-stream
description: 'Analyze whether a given C/C++ code snippet follows the recommended coding style for array-to-stream conversion in Vitis HLS. Keywords: array, stream, HLS, pragma, ap_fifo, axis, interface'
license: MIT
argument-hint: "[<TOP_FUNCTION — top-level HLS function name e.g. 'Kernel'>]"
metadata:
  author: "Huina Li, Alex Li"
  version: "1.0.0"
---

> **VibeSys note:** in a VibeSys run the task evaluator runs every Vitis flow (C simulation, synthesis, co-simulation, implementation) and returns its reports as evaluation evidence. Take the top function from the task's candidate contract. The skills this text names for running flows or reading reports (`hls-run-flow`, `hls-synth-report`, `hls-component-basic-info`, `hls-optimize`, `hls-loop-unrollable`) are not installed; use the evaluator's reports and the task contract instead.
<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->
You are focused on analyzing whether code follows the recommended coding style for array-to-stream conversion.

#### Concepts:
- **Array-to-stream conversion**: Converting arrays to streaming interfaces for efficient data transfer
- **Stream pragma**: `#pragma HLS stream variable=A type=T depth=D`
- **Interface pragma**: `#pragma HLS interface [mode=ap_fifo|axis] port=A` (`mode` is optional; for example, `mode=ap_fifo` or just `ap_fifo`)
- **Top function**: the top-level HLS function, get it via argument `<TOP_FUNCTION>`, if not provided, call `/hls-component-basic-info` skill to get top_function which is the top-level function name
- **dataflow region**: dataflow region is a function or for-loop that contains a #pragma HLS dataflow.
- **Top function arguments**: Parameters of the top-level function

#### Scope:
- Strictly follow rules 1–6; do not extend
- Must get `<TOP_FUNCTION>` value which is the top-level function name (e.g. `Kernel`)

---

#### Analysis Steps:

**1. Pragma Presence**
   Validate that the code contains at least one of the following two array-to-stream pragmas:
   - Stream pragma: `#pragma HLS stream variable=A type=T depth=D`
   - Interface pragma: `#pragma HLS interface [mode=ap_fifo|axis] port=A` (`mode` is optional; for example, `mode=ap_fifo` or just `ap_fifo`)

**2. Stream Pragma Rules** (check if `#pragma HLS stream variable=<VAR> [type=<T>] [depth=<D>]` is used)
   - **2.1** It must be a legal HLS stream pragma with the form `#pragma HLS stream variable=<VAR> [type=<T>] [depth=<D>]`
   - **2.2** `<VAR>` can be:
     - An array (single or multi-dimensional)
     - A scalar variable
   - **2.3** `<VAR>` must be a local variable or a global variable
   - **2.4** `<VAR>` must be passed within the dataflow region
   - **2.5** `<VAR>` cannot be a top-level function argument
   - **2.6** `type=<T>` is optional:
     - If omitted, defaults to "fifo"
     - If provided, `<T>` must be "fifo"
   - **2.7** `depth=<D>` is optional:
     - If omitted, uses default depth
     - If provided, `<D>` must be a positive integer literal or const expression that evaluates to a positive integer

**3. Interface Pragma Rules** (check if `#pragma HLS interface [mode=ap_fifo|axis] port=A` is used; `mode` is optional; for example, `mode=ap_fifo` or just `ap_fifo`)
   - **3.1** `A` must be an array (possibly multi-dimensional) or a scalar
   - **3.2** `A` must be passed within the dataflow region
   - **3.3** `A` must be a top function argument
   - **3.4** `mode` must be either `ap_fifo` or `axis`

**4. Struct Element Type Rules** (skip if array element type is not a struct type)
   If the array element type is a struct type, ensure that the whole struct is prepared in a separate struct variable and then stored at once in the array.

**5. Struct Copy Assignment Rules** (skip if array element type is not a struct type)
   If the array element type is a struct type, ensure that no custom copy assignment operator is defined (such as `A& operator=(const A& a)`) except mark it as default (such as `A& operator=(const A& a) = default`) for that struct type.

**6. Stream Pragma Access Pattern Rules** (check only if stream pragma `#pragma HLS stream variable=A type=T depth=D` is used)
   Ensure that each accessed array element is read exactly once, written exactly once, and that the read/write order is consistent:
   - **6.1** Ensure that each element is accessed exactly once and not more than once (no repeated access to any element)
   - **6.2** Ensure that the array is accessed in the same order and range on both reading and writing sides
   - **6.3** Ensure the iteration order must be the same (e.g., both ascending or both descending)
   - **6.4** Each side must access the same number of elements (e.g., if write accesses N-1 elements, read must also access N-1 elements, not N)

---

#### Verdict:
If **all** applicable criteria are met, the code follows the **recommended coding style for array-to-stream conversion**. Otherwise, it does **not** follow the recommended coding style.



---

## When Asked to Fix Code to Follow Array-to-Stream Style

If the user asks you to **fix** code so it follows the recommended array-to-stream style:

1. Identify the failing rule(s)
2. Apply the minimal fix while preserving behavior
3. Output the **complete fixed code** in a fenced code block:
   ````
   ```cpp
   // fixed code here
   ```
   ````
