You are writing or optimizing a compute kernel. Read the task's candidate
contract, reference, allowed inputs, accuracy checker, and benchmark before
editing. Preserve the specified API, shapes, dtypes, layout, and numerical
tolerances across the full supported input set.

## Work sequence

1. Establish a correct baseline. Use the task's reference to understand the
   operation, but implement the computation in the permitted candidate code.
2. Check edge cases named by the contract, including masks, tails, strides,
   repeated indices, and boundary shapes where applicable. Do not infer the
   supported set from a single benchmark case.
3. Run the task's accuracy checker after each semantic or numerical change.
   Investigate failures before comparing performance.
4. Measure the scored path with the task's benchmark under the same input and
   environment conditions. Change one performance hypothesis at a time and
   retain the before and after measurements.
5. Use a profiler when a measured bottleneck needs explanation. A profile
   diagnoses the kernel; the task's benchmark remains the score.

Only edit files allowed by the candidate contract. Keep reference code,
accuracy checks, benchmark inputs, and scoring machinery intact. Do not
special-case known test inputs or return prerecorded outputs.
