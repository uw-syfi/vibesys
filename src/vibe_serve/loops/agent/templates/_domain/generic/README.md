# Generic (no domain context)

**Use for:** a target that needs no special background knowledge or review gates
beyond the task statement, the modality contract, and the run's pass criteria.

This domain injects **no** prose into the base prompts — there are no role
files in this directory, so the neutral base prompts render unchanged. It is the
recommended starting point for adding a registered domain: copy it into a new
in-repo domain prompt directory, add `implementer.md`, `judge.md`, and
optionally `single_agent.md`, then register the domain in source. See
`../README.md`.
