package lifecycle

import (
	"time"

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

// Limit is the longest the launch's command can run, and zero when the launch does not say.
func (l Launch) Limit() time.Duration {
	if c, ok := l.Spec.Settings.(core.CommandSettings); ok {
		return c.Timeout
	}
	return 0
}

// Outcome is the one report a supervisor makes about its job: how it ended, and whether a cancel
// reached the supervisor first.
type Outcome struct {
	// End is how the job ended. An Outcome with no End says nothing, which is never a success.
	End End
	// CancelAsked says a cancel request reached the supervisor before the command ended, and
	// CancelReason is its text, which may be empty. A command that ended on its own first still
	// reports how it ended.
	CancelAsked  bool
	CancelReason string
}

// End is how a job ended, one variant of SupervisorOutcome's result. The set is closed.
type End interface {
	end()
}

// Ran says the command ran, or was killed, and how it ended.
type Ran struct{ Exec core.ExecOutcome }

// NotAttempted says the command could not be attempted, such as a checkout that failed.
type NotAttempted struct{ Failure core.Failure }

// Passed says a check or a measure job passed, and holds the result it found.
type Passed struct{ Result core.Result }

// Rejected says every command ran and the job has no acceptable result.
type Rejected struct{ Reason string }

func (Ran) end()          {}
func (NotAttempted) end() {}
func (Passed) end()       {}
func (Rejected) end()     {}
