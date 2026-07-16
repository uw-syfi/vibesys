# Resources

This directory contains assets that VibeSys owns and exposes to agents or
their execution environments, but that are not part of the `vibesys` Python
package and are not standalone example workloads.

Resources may be copied into an agent workspace, mounted or linked read-only,
uploaded to a remote environment, or exposed through a service. The directory
describes ownership and purpose; it does not imply one materialization method.
The environment integration that consumes a resource owns that decision.

Put files here when they are reusable, framework-owned inputs to an agent run,
including:

- `profilers/`: profiler MCP servers and analysis helpers that VibeSys
  materializes in the workspace for the selected profiler.
- `skills/`: bundled Agent Skills and reference material exposed to agents.

Keep application behavior and packaged prompt templates under
`src/vibesys/`, reusable Python libraries under `libs/`, standalone workload
bundles under `examples/`, and outputs produced by a run in its artifact or log
directories.
