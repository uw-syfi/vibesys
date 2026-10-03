# vs-faults

Test support: seeded, declarative fault injection at VibeSys's boundary
interfaces. One `FaultPlan` (a seed plus rules: boundary, target, the ordinal
of the matching call, the fault) drives three wrappers, each implementing the
interface it wraps:

- `FaultyAgentClient` wraps any `AgentClientProtocol`: crash, turn timeout,
  malformed output, schema-invalid output, extra keys, and schema-valid but
  wrong replies. Replies are generated from the schema each turn declares, so
  the wrapper knows no roles.
- `FaultyToolDispatch` wraps a tool dispatcher keyed by tool name: error
  result, dropped call, reply lost after the server ran it, duplicate delivery.
- `python -m vs_faults.connector PLAN STATE -- INNER...` wraps a Slurm
  connector command: ssh down, failing Slurm commands, killed jobs (OOM,
  preemption, node failure), garbage `squeue`/`sacct` output.

An empty plan makes every wrapper a pass-through. Calls are counted per
boundary and target, so a schedule does not depend on wall time.
