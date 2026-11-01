// Package backend declares Backend, facts about the compute hardware a task targets.
//
// A backend is the compute stack (cpu, cuda, rocm, metal, trainium), as in the Python product's
// compute backends. This version supports the CPU stack. Which environment a backend allows,
// such as Metal needing the host, is a pure rule in internal/core. Accelerator passthrough and
// device pinning are out of scope and arrive as separate optional interfaces, not as flags here.
package backend

import (
	"context"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Backend reads the machine a job runs on.
type Backend interface {
	// Kind is the compute stack this backend serves.
	Kind() core.BackendKind

	// Machine returns what does not change while the daemon runs: host identity, operating
	// system, CPU and memory. Results record it.
	Machine(ctx context.Context) (core.MachineFacts, error)

	// Load samples the machine's current load. Results record it before and after a measurement.
	// Load is reported and never acted on: VibeSys serializes its own jobs, and other load on the
	// machine is neither detected nor prevented.
	Load(ctx context.Context) (core.MachineLoad, error)
}
