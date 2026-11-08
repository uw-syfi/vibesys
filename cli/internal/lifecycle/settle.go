package lifecycle

import (
	"fmt"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Settlement is the change a finished, abandoned or cut-off job gets: the transition for its store,
// and the line to add to its output when the outcome alone does not say why.
type Settlement struct {
	Transition core.Transition
	// Log is empty when the job's own output tells the story.
	Log string
}

// Requeues says the settlement queues the job again, for another attempt, and does not end it.
func (s Settlement) Requeues() bool { return s.Transition.Cut != nil }

// Settle says what a supervisor's outcome means for its job, which was in state from. Nothing is
// inferred: an outcome that does not say the job succeeded is not a success.
//
//   - A command job whose command exited with 0 succeeded, and one that exited with another code
//     failed with that code. A timeout, a service that never became ready and a service that
//     exited early fail the job and say so in its output.
//   - A check or a measure job that passed succeeds with its result. One whose step stopped is
//     settled as a command that ended that way, except that an exit code of 0 is no pass; one whose
//     result was rejected fails, with the reason in its output.
//   - A command ended by a cancel that reached the supervisor is cancelled, with that reason. One
//     ended the same way with no request behind it, because the supervisor was stopped from
//     outside, is lost: the user did not cancel it and it did not finish.
//   - A command that never started failed, with the reason in its output: nothing of it ran.
//   - A command that started and was not seen to end is lost, with the reason in its output: it
//     may have done anything, so it is neither a failure to run nor a result.
//   - An outcome that says none of these is lost.
func Settle(job core.JobID, from core.JobState, o Outcome, at time.Time) Settlement {
	t := core.Transition{Job: job, From: from, At: at}
	switch end := o.End.(type) {
	case CommandEnded:
		return settleAttempt(t, end.Attempt, o)
	case FlowEnded:
		return settleVerdict(t, end.Verdict, o)
	default:
		return unknownEnd(t)
	}
}

// settleAttempt says what the end of a command job's attempt means.
func settleAttempt(t core.Transition, a core.Attempt, o Outcome) Settlement {
	switch a := a.(type) {
	case core.Ended:
		return settleRan(t, a.Exec, o)
	case core.NotStarted:
		return notStarted(t, a.Failure)
	case core.Unobserved:
		return unobserved(t, a.Failure)
	default:
		return unknownEnd(t)
	}
}

// settleVerdict says what the end of a check or a measure job means.
func settleVerdict(t core.Transition, v Verdict, o Outcome) Settlement {
	switch v := v.(type) {
	case Passed:
		code := 0
		result := v.Result
		t.To, t.ExitCode, t.Result = core.JobSucceeded, &code, &result
		// The check pass a measurement links is in the result, and the job takes it from there:
		// it is stored once, so the transition does not carry a second copy to disagree with it.
		return Settlement{Transition: t}
	case Stopped:
		if v.Exec.Kind == core.ExecExited && v.Exec.ExitCode == 0 {
			// Only Passed is a pass: a step that exited 0 did not stop the job.
			t.To = core.JobLost
			return Settlement{Transition: t, Log: "vibesys-cli: the supervisor said a step stopped the job and exited 0, which is no ending"}
		}
		s := settleRan(t, v.Exec, o)
		if s.Transition.To == core.JobFailed {
			s.Transition.FailedStage = v.Stage
		}
		return s
	case NotAttempted:
		return notStarted(t, v.Failure)
	case Rejected:
		t.To, t.FailedStage = core.JobFailed, v.Stage
		return Settlement{Transition: t, Log: fmt.Sprintf("vibesys-cli: the job's result was rejected: %s", v.Reason)}
	case Lost:
		return unobserved(t, v.Failure)
	default:
		return unknownEnd(t)
	}
}

func notStarted(t core.Transition, f core.Failure) Settlement {
	t.To = core.JobFailed
	return Settlement{Transition: t, Log: fmt.Sprintf("vibesys-cli: the job could not be run: %s", f.Message)}
}

func unobserved(t core.Transition, f core.Failure) Settlement {
	t.To = core.JobLost
	return Settlement{Transition: t, Log: fmt.Sprintf("vibesys-cli: the job ran, and how it ended is not known: %s", f.Message)}
}

func unknownEnd(t core.Transition) Settlement {
	t.To = core.JobLost
	return Settlement{Transition: t, Log: "vibesys-cli: the supervisor did not say how the job ended"}
}

// settleRan says what a command's end means.
func settleRan(t core.Transition, exec core.ExecOutcome, o Outcome) Settlement {
	switch exec.Kind {
	case core.ExecExited:
		code := exec.ExitCode
		t.ExitCode = &code
		t.To = core.JobFailed
		if code == 0 {
			t.To = core.JobSucceeded
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
		return Settlement{Transition: t, Log: fmt.Sprintf("vibesys-cli: the job ended in a way this build does not know (%q)", exec.Kind)}
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

// stoppedJob is the end of a running job the daemon gave up following and whose command it ended.
func stoppedJob(job core.JobID, f core.Failure, at time.Time) Settlement {
	return Settlement{
		Transition: core.Transition{Job: job, From: core.JobRunning, To: core.JobFailed, At: at},
		Log:        fmt.Sprintf("vibesys-cli: the job was stopped because the daemon could not follow it: %s", f.Message),
	}
}

// lostJob is the end of a running job whose supervisor is gone, which is not run again.
func lostJob(job core.JobID, at time.Time) Settlement {
	return Settlement{
		Transition: core.Transition{Job: job, From: core.JobRunning, To: core.JobLost, At: at},
		Log:        "vibesys-cli: the job's supervisor went away, so the job is lost and is not run again",
	}
}

// CutOff says what comes of a running job whose supervisor is proved gone: the job with policy had
// attempts cut off before this one, proof is how the supervisor is known gone, and start what is
// known of whether its command was told to start.
//
//   - A command that was never told to start ran nothing, so the job is queued again.
//   - A command that may have started is queued again only under RerunAfterProvenDeath, the policy
//     of a check or a measure job, which saves nothing until it passes.
//   - Either way only while the job has had fewer than core.MaxAttempts attempts, this one
//     included: at the bound it is lost.
//   - A supervisor known gone only because it was released proves no cut: the job is lost.
//
// A nil start may have started, and an unknown policy reruns nothing after a start.
func CutOff(job core.JobID, policy core.RerunPolicy, attempts int, proof DeathProof, start Start, at time.Time) Settlement {
	var cause core.CutCause
	switch proof {
	case ProofLockFree:
		cause = core.CutSupervisorDied
	case ProofRebooted:
		cause = core.CutRebooted
	case ProofReleased:
		return lostJob(job, at)
	default:
		return lostJob(job, at)
	}
	attempt := attempts + 1
	var cut core.Cut
	switch start.(type) {
	case NeverStarted:
		cut = core.CutBeforeStart{By: cause}
	case MayHaveStarted, nil:
		if policy != core.RerunAfterProvenDeath {
			return Settlement{
				Transition: core.Transition{Job: job, From: core.JobRunning, To: core.JobLost, At: at},
				Log:        fmt.Sprintf("vibesys-cli: the job's supervisor went away (%s) after its command may have started, so the job is lost and is not run again: a command job is never run twice", cause),
			}
		}
		cut = core.CutMidCommand{By: cause}
	}
	if attempt >= core.MaxAttempts {
		return Settlement{
			Transition: core.Transition{Job: job, From: core.JobRunning, To: core.JobLost, At: at},
			Log:        fmt.Sprintf("vibesys-cli: attempt %d was cut off %s (%s), and a job gets %d attempts, so the job is lost and is not run again", attempt, howCut(cut), cause, core.MaxAttempts),
		}
	}
	return Settlement{
		Transition: core.Transition{Job: job, From: core.JobRunning, To: core.JobQueued, At: at, Cut: cut},
		Log:        CutLine(attempt, cut),
	}
}

// CutLine is the line a job's output gets for its attempt number attempt, which was cut off and
// queued again. It is made from what the store records of the attempt alone, so a daemon that
// finds the line missing writes the same one.
func CutLine(attempt int, cut core.Cut) string {
	return fmt.Sprintf("vibesys-cli: attempt %d was cut off %s (%s); running it again as attempt %d of %d", attempt, howCut(cut), cut.Cause(), attempt+1, core.MaxAttempts)
}

func howCut(cut core.Cut) string {
	switch cut.(type) {
	case core.CutBeforeStart:
		return "before its command started"
	case core.CutMidCommand:
		return "mid-command"
	}
	return "for no recorded reason"
}

// givenUpJob is a running job whose store gave up the place that ran it, with nothing here that can
// be running its command. The user's word proves no death, so the job is not run again.
func givenUpJob(job core.JobID, at time.Time) Settlement {
	return Settlement{
		Transition: core.Transition{Job: job, From: core.JobRunning, To: core.JobLost, At: at},
		Log:        "vibesys-cli: the store's owner record was removed, which gives up the place that ran the job, and nothing here runs it, so the job is lost and is not run again",
	}
}
