# Generic (no domain context)

**Use for:** a target that needs no special background knowledge or review gates
beyond the task statement, the modality contract, and the run's pass criteria.

This domain injects **no** prose into the base prompts — there are no role
sections below, so the neutral base prompts render unchanged. It is the
recommended starting point to copy when authoring your own domain: add
`## implementer`, `## judge`, and optionally `## single_agent` sections. See
`./README.md`.
