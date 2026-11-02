package lifecycle

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Launch is what a supervisor is started with: a job as the daemon resolved it. The daemon
// decides the environment and the timeout before it writes a Launch, so a supervisor decides
// nothing: Spec.Environment is a concrete kind and a command's timeout is positive.
type Launch struct {
	Repository string
	Task       string
	Job        core.JobID
	Spec       core.JobSpec
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
}
