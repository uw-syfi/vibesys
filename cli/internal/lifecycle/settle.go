package lifecycle

import (
	"fmt"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Settlement is the change a finished or abandoned job gets: the transition for its store, and
// the line to add to its output when the outcome alone does not say why.
type Settlement struct {
	Transition core.Transition
	// Log is empty when the job's own output tells the story.
	Log string
}

// Settle says what a supervisor's outcome means for its job, which was in state from. Nothing is
// inferred: an outcome that does not say the command succeeded is not a success.
//
//   - A command that exited with 0 succeeded, and one that exited with another code failed with
//     that code. A timeout, a service that never became ready and a service that exited early
//     fail the job and say so in its output.
//   - A command ended by a cancel that reached the supervisor is cancelled, with that reason. One
//     ended the same way with no request behind it, because the supervisor was stopped from
//     outside, is lost: the user did not cancel it and it did not finish.
//   - An outcome the environment could not observe, and one this build does not know, is lost.
//   - A command that could not be attempted failed, with the reason in its output.
//   - A check or a measure job that passed has its result in the outcome and succeeds with it;
//     one whose result was rejected fails; and one that failed says in its output which step did.
func Settle(job core.JobID, from core.JobState, o Outcome, at time.Time) Settlement {
	s := settle(job, from, o, at)
	if o.Reason != "" && s.Transition.To == core.JobFailed {
		s.Log = "vibesys-cli: " + o.Reason
	}
	return s
}

func settle(job core.JobID, from core.JobState, o Outcome, at time.Time) Settlement {
	t := core.Transition{Job: job, From: from, At: at}
	if o.NotAttempted != nil {
		t.To = core.JobFailed
		return Settlement{Transition: t, Log: fmt.Sprintf("vibesys-cli: the job could not be run: %s", o.NotAttempted.Message)}
	}
	if o.Rejected {
		t.To = core.JobFailed
		return Settlement{Transition: t, Log: "vibesys-cli: the job's result was rejected"}
	}
	switch o.Exec.Kind {
	case core.ExecExited:
		code := o.Exec.ExitCode
		t.ExitCode = &code
		t.To = core.JobFailed
		if code == 0 {
			t.To = core.JobSucceeded
			if o.Result != nil {
				t.Result = o.Result
				if m, ok := o.Result.Outcome.(core.MeasureOutcome); ok {
					t.CheckJob = m.CheckJob
				}
			}
		}
		return Settlement{Transition: t}
	case core.ExecTimedOut:
		t.To = core.JobFailed
		return Settlement{Transition: t, Log: "vibesys-cli: the command ran past its time limit and was ended"}
	case core.ExecServiceNotReady:
		t.To = core.JobFailed
		return Settlement{Transition: t, Log: "vibesys-cli: the service was not ready in time"}
	case core.ExecServiceExited:
		t.To = core.JobFailed
		return Settlement{Transition: t, Log: "vibesys-cli: the service exited before the command finished"}
	case core.ExecCancelled:
		if o.CancelAsked {
			t.To = core.JobCancelled
			t.CancelReason = o.CancelReason
			return Settlement{Transition: t}
		}
		t.To = core.JobLost
		return Settlement{Transition: t, Log: "vibesys-cli: the job was ended by something other than a cancel"}
	default:
		t.To = core.JobLost
		return Settlement{Transition: t, Log: "vibesys-cli: the environment could not say how the job ended"}
	}
}

// cancelQueued is the end of a job that was cancelled before it ran.
func cancelQueued(job core.JobID, reason string, at time.Time) Settlement {
	return Settlement{Transition: core.Transition{Job: job, From: core.JobQueued, To: core.JobCancelled, At: at, CancelReason: reason}}
}

// failedToLaunch is the end of a job whose supervisor could not be started.
func failedToLaunch(job core.JobID, f core.Failure, at time.Time) Settlement {
	return Settlement{
		Transition: core.Transition{Job: job, From: core.JobQueued, To: core.JobFailed, At: at},
		Log:        fmt.Sprintf("vibesys-cli: the job could not be started: %s", f.Message),
	}
}

// lostJob is the end of a running job whose supervisor is gone.
func lostJob(job core.JobID, at time.Time) Settlement {
	return Settlement{
		Transition: core.Transition{Job: job, From: core.JobRunning, To: core.JobLost, At: at},
		Log:        "vibesys-cli: the job's supervisor went away, so the job is lost and is not retried",
	}
}
