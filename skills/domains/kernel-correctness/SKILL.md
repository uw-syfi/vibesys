---
name: kernel-correctness
description: Check a compute kernel against its task reference, including numerical tolerance, memory safety, and supported input variants. Use for kernel implementation or correctness failures.
---

# Kernel correctness

Read the task's candidate contract and accuracy checker to identify the exact
supported inputs and comparison rule. Translate the reference operation into
invariants for every output element, including reductions, masks, aliasing,
strides, and tails where applicable. Keep the evaluator-owned reference and
checker unchanged.

When a case fails, isolate the smallest supported shape and input pattern that
reproduces it. Compare intermediate values or one output tile against the
reference, then distinguish indexing errors, missing synchronization,
uninitialized memory, and allowed floating-point variation. Run the full
checker after each fix; a passing benchmark shape is insufficient.

For parallel writes, establish which thread owns each address. For reductions,
check accumulation dtype, order-sensitive tolerance, and behavior at zero or
masked elements. If the contract requires repeatable output, verify repeated
runs under the same input and reject race-dependent results.
