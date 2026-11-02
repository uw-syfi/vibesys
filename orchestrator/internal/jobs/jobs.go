// Package jobs is the orchestrator's view of vibesys-cli's jobs.
//
// JobSource is level-triggered: it says where a run's jobs stand now, not what changed. The shell
// asks on a timer and feeds each update to the core, which tells the lead of a finished job once
// however often it hears of it. So a restart has nothing to resume and a missed poll loses nothing:
// the next one says the same.
package jobs

import (
	"context"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// JobSource reports a run's jobs.
type JobSource interface {
	// Jobs returns the run's jobs as they stand, each once, in job id order. A job in a final state
	// stays listed in that state. An error means nothing is known this time, and says nothing about
	// any job.
	Jobs(ctx context.Context, run core.Run) ([]core.JobUpdate, error)
}
