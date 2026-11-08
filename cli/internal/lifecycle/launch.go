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
	// OutputBase is where this attempt's output starts in the job's stored output, which holds what
	// earlier attempts of a job queued again wrote. The supervisor's output offsets count from it.
	OutputBase uint64
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

// End is how a job ended, and its variant is the job's kind: a command job ends in CommandEnded
// and a check or a measure job in FlowEnded. Only a FlowEnded carries a result, and only a
// CommandEnded an exit code that alone means success, so neither kind's ending can be read as the
// other's. The set is closed.
type End interface {
	end()
}

// CommandEnded is how a command job's one attempt went, as its environment said it: never
// started, ended in a way the environment saw, or started and not seen to end.
type CommandEnded struct{ Attempt core.Attempt }

// FlowEnded is how a check or a measure job ended.
type FlowEnded struct{ Verdict Verdict }

func (CommandEnded) end() {}
func (FlowEnded) end()    {}

// Verdict is how a check or a measure job ended. The set is closed, and Passed is the only one
// that succeeds.
type Verdict interface {
	verdict()
}

// Passed says the job passed, and holds the result it found.
type Passed struct{ Result core.Result }

// Stopped says a step's command ended unlike a pass: an exit code other than 0, a time limit, a
// cancel.
type Stopped struct {
	Exec core.ExecOutcome
	// Stage is the stage the step belonged to, empty for a step outside the stages of a job's record.
	Stage core.JobStage
}

// NotAttempted says a step's command never started, such as a checkout that failed.
type NotAttempted struct{ Failure core.Failure }

// Rejected says every command ran and the job has no acceptable result.
type Rejected struct {
	Reason string
	// Stage is the stage that produced the output that was refused.
	Stage core.JobStage
}

// Lost says a step's command started and nobody can say how it ended.
type Lost struct{ Failure core.Failure }

func (Passed) verdict()       {}
func (Stopped) verdict()      {}
func (NotAttempted) verdict() {}
func (Rejected) verdict()     {}
func (Lost) verdict()         {}
