# Kernel-writing domain prompts

Use for tasks that implement or optimize compute kernels against a task-owned
reference and scored benchmark. Select with `[agent].domain = "kernel-writing"`
in the input bundle. The domain is independent of a particular kernel DSL,
model architecture, or profiler.

The role files provide kernel-specific correctness, performance, and profiling
guidance. The task bundle owns its input shapes, numerical tolerances, permitted
files, checker, and benchmark. The bundled kernel skills are selected by domain
through `resources/skills/.vibesys.toml`.
