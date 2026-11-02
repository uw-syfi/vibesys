package lifecycle

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
)

// Launch is what a supervisor is started with: a job as the daemon resolved it. The daemon
// decides the environment and the timeout before it writes a Launch, so a supervisor decides
// nothing: Spec.Environment is a concrete kind and a command's timeout is positive.
type Launch struct {
	Repository string
	Task       string
	Job        core.JobID
	Spec       core.JobSpec
	// Plan is what a check or a measure job runs, as the daemon decided it when the job started.
	// It is nil for a command job, which the spec alone describes.
	Plan *flow.Plan
}

// Ref is the job's identity.
func (l Launch) Ref() Ref {
	return Ref{Store: StoreIDOf(l.Repository, l.Task), Job: l.Job}
}

// Outcome is the one report a supervisor makes about its job.
type Outcome struct {
	// Exec is how the command ended. It is meaningful when NotAttempted is nil.
	Exec core.ExecOutcome
	// NotAttempted is set when the command could not be attempted, such as a checkout that failed.
	NotAttempted *core.Failure
	// CancelAsked says a cancel request reached the supervisor before the command ended, and
	// CancelReason is its text, which may be empty. A command that ended on its own first still
	// reports how it ended.
	CancelAsked  bool
	CancelReason string

	// The rest is for a check or a measure job, which the supervisor drives through a plan.
	//
	// Result is the result the job found. It is set exactly when the job passed, and Exec is then
	// a plain exit 0.
	Result *core.Result
	// Rejected says every command ran but the job has no acceptable result, such as a benchmark
	// that wrote none. Reason says why.
	Rejected bool
	// Reason is a line for the job's output saying how the job ended, when it did not pass.
	Reason string
}
