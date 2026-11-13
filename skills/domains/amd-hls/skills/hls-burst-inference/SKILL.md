---
name: hls-burst-inference
description: 'Analyze whether burst inference optimization can be applied to a given C/C++ code snippet in Vitis HLS. Keywords: burst, HLS, pragma, m_axi, AXI, memory, optimization'
license: MIT
argument-hint: "[<TOP_FUNCTION — top-level HLS function name e.g. 'Kernel'>]"
metadata:
  author: "Alex Li"
  version: "1.0.0"
---

> **VibeSys note:** in a VibeSys run the task evaluator runs every Vitis flow (C simulation, synthesis, co-simulation, implementation) and returns its reports as evaluation evidence. Take the top function from the task's candidate contract. The skills this text names for running flows or reading reports (`hls-run-flow`, `hls-synth-report`, `hls-component-basic-info`, `hls-optimize`, `hls-loop-unrollable`) are not installed; use the evaluator's reports and the task contract instead.
<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->
You are focused on analyzing whether burst inference optimization can be applied.
You only answer questions related to Burst Inference.

#### Concepts:
- **Burst inference**: Automatic optimization that combines multiple sequential memory accesses into a single burst transaction
- **M-AXI interface**: Memory-mapped AXI interface for accessing external memory
- **Burst inference region**: Each non-inlined function, the top function, or a top loop is an independent region. Code before the first top-level loop and code after any top-level loop within a function are also independent regions
- **Top function**: the top-level HLS function, get it via argument `<TOP_FUNCTION>`, if not provided, call `/hls-component-basic-info` skill to get top_function which is the top-level function name
- **Vivado flow**: Default flow where top arguments without interface pragma are NOT M-AXI
- **Vitis flow**: Flow where top arguments without interface pragma ARE M-AXI by default

#### Scope:
- **Global rules** (Preconditions 1–4 and Limitations 1–2) must be checked globally, NOT per region
- **Region-local rules** (Preconditions 5–9 and Limitations 3–6) MUST be checked independently within each burst inference region
- Strictly follow rules 1–9 for preconditions and 1–6 for limitations; do not extend

---

#### Analysis Steps:

**1. M-AXI Interface Check (Global)**
   Validate that the variable must be an M-AXI top argument:
   - **1.1** Check if there is an interface pragma `#pragma HLS interface m_axi` on the variable
   - **1.2** If no interface pragma:
     - Under **Vivado flow**: top arguments are NOT M-AXI (burst inference not applicable)
     - Under **Vitis flow**: top arguments ARE M-AXI by default (burst inference may apply)

**2. DATAFLOW Pragma Check (Global)**
   - **2.1** Only judge burst inference as **disallowed** if the DATAFLOW pragma appears as a direct statement within the loop block itself
   - **2.2** If the DATAFLOW pragma is located within any nested block (such as an inner loop or other nested scope inside the loop), burst inference is **allowed** for the outer loop
   - **2.3** If the DATAFLOW pragma is outside the loop, burst inference is **allowed** for the loop

**3. Data Type Width Check (Global)**
   - **3.1** The width of the accessed data type must be a power of 2
   - **3.2** If the accessed data type has an `aligned` attribute, always use the aligned size instead of the natural size for this check

**4. Volatile Qualifier Check (Global)**
   Validate that the variable must NOT have the `volatile` qualifier. The volatile qualifier prevents burst access.

**5. Burst Length Check (Region-Local)**
   - **5.1** The burst length must be greater than 1 (at least 2 accesses)
   - **5.2** Do NOT reject burst inference only because the burst size is small—as long as burst length is 2 or more, it is allowed

**6. Memory Access Pattern Check (Region-Local)**
   Validate that accesses must be consecutive in memory and in monotonically increasing order:
   - **6.1** Accesses must be one next to another with no gaps or overlap, in forward order
   - **6.2** It is allowed to access only a contiguous subset (e.g., `out[0]` to `out[var-1]`) as long as that subset has no skipped elements
   - **6.3** The start or end of the access region does not have to cover the entire array—only the accesses in a burst must be strictly contiguous
   - **6.4** When checking the consecutive/non-overlapping rule, analyze the **combined index ranges of ALL memory accesses** to the SAME variable within the loop
   - **6.5** If there is any overlap between accesses (e.g., both `a[i]` and `a[i+offset]` might touch the same address during different iterations), burst inference CANNOT be applied, even if each individual expression is strictly increasing and contiguous

**7. Burst Length/Step Determinability Check (Region-Local)**
   - **7.1** The number of read/write accesses (burst length) must be determinable before the request is sent out
   - **7.2** Burst length/step can be computed at runtime (expression), but must be computed before the read/write request is issued
   - **7.3** Do NOT require burst length/step to be a compile-time constant—dynamically provided values via function arguments or runtime variables are allowed

**8. Same Direction Access Check (Region-Local)**
   - **8.1** If there are same-direction accesses (all reads or all writes) on the same channel of the same bundle in the same region, burst inference will NOT apply to any of these accesses
   - **8.2** If there are multiple m_axi interfaces on the same channel and same bundle, and they are all reads or all writes, burst will not happen for any of them

**9. Dependency Check (Region-Local)**
   - **9.1** There must be no dependency issues from the time a burst request is initiated until finished
   - **9.2** If there are load/store operations on the same address under the same region (e.g., `a[i] += 5` or `b[i] = a[i], a[i] = c[i]`), burst will NOT happen

---

#### Burst Limitations (MAY FAIL):

These conditions may cause burst inference to fail even if preconditions are met:

**Limitation 1. ap_int/ap_uint Loop Induction Variables (Global)**
   Usage of `ap_int`/`ap_uint` types as loop induction variables may fail burst inference.

**Limitation 2. Shared M-AXI Port in Dataflow (Global)**
   If multiple tasks or functions inside a dataflow region access the same M-AXI port (same port and bundle), burst inference may FAIL—even if there is no address range overlap—because tasks might initiate parallel transactions. Burst is generally not supported on shared ports unless static analysis can prove safe sequencing.

**Limitation 3. Loop Dependencies (Region-Local)**
   Inter and intra loop dependencies on the variables may fail burst inference.

**Limitation 4. Cross-Function Access (Region-Local)**
   If memory accesses occur across function boundaries and the index is passed in as a parameter, and the subfunction containing the access is inline off, burst inference may fail;
   If memory accesses occur across function boundaries and the index is passed in as a parameter, and the subfunction containing the access is inline, burst inference can be applied;

**Limitation 5. Potential Address Overlap (Region-Local)**
   If there are load/store operations on potentially the same address under the same region, burst may not happen (e.g., if reading `a[i]` and writing `a[j]`, burst may fail unless `i != j` can be proven).

---

#### Verdict:
If **all preconditions** (1–9) are met and no **limitations** (1–5) apply, burst inference optimization **can be applied**. Otherwise, it **cannot** be applied.

---

## When Asked to Fix Code for Burst Inference

If the user asks you to **fix** code so that burst inference can be applied:

1. Identify the failing precondition(s)
2. Apply the minimal fix to satisfy all preconditions while preserving behavior
3. Output the **complete fixed function(s)** in a fenced code block:
   ````
   ```cpp
   // fixed code here
   ```
   ````
