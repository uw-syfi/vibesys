package core

import "time"

// WaitFor is what a wait waits for.
type WaitFor struct {
	IDs []JobID
	// Any ends the wait when one job is final, and not when all are.
	Any bool
	// Timeout ends the wait with WaitTimedOut after this long. Zero waits as long as it takes. It
	// never cancels a job.
	Timeout time.Duration
}

// WaitEnd is how a wait ended. The exit code of `wait` is a function of it alone.
type WaitEnd string

// The ends of a wait.
const (
	// WaitAllSucceeded is every job final and succeeded.
	WaitAllSucceeded WaitEnd = "all_succeeded"
	// WaitSomeFailed is every job final and one that did not succeed.
	WaitSomeFailed WaitEnd = "some_failed"
	// WaitFirstSucceeded is, for a wait on any job, a final job and every final job succeeded.
	WaitFirstSucceeded WaitEnd = "first_succeeded"
	// WaitFirstFailed is, for a wait on any job, a final job that did not succeed.
	WaitFirstFailed WaitEnd = "first_failed"
	// WaitTimedOut is a timeout that passed first. It cancels no job.
	WaitTimedOut WaitEnd = "timed_out"
)

// WaitOutcome is how a wait ended.
type WaitOutcome struct {
	End WaitEnd
	// DecidedBy is the job that decided the end: for WaitSomeFailed and WaitFirstFailed the first,
	// in the order asked, that did not succeed, and for WaitFirstSucceeded the first final one. It
	// is empty for WaitAllSucceeded, where every job did, and for WaitTimedOut.
	DecidedBy JobID
	// Jobs is the state of each job when the wait ended, in the order asked.
	Jobs []Job
}

// DecideWait says whether a wait on the jobs, in the order asked, is over, and how it ended. A wait
// for all jobs is over when every one is final; with any, when one is. A failed, cancelled or lost
// job is not a success. It is the one copy of this rule: the daemon applies it and sends the
// outcome, and no client decides it again. A wait on no jobs is never over; callers reject it as
// input before they ask.
func DecideWait(jobs []Job, any bool) (WaitOutcome, bool) {
	var firstFinal, firstFailed *Job
	final := 0
	for i := range jobs {
		j := &jobs[i]
		if !j.State.Final() {
			continue
		}
		final++
		if firstFinal == nil {
			firstFinal = j
		}
		if firstFailed == nil && j.State != JobSucceeded {
			firstFailed = j
		}
	}
	out := WaitOutcome{Jobs: jobs}
	switch {
	case final == 0:
		return WaitOutcome{}, false
	case any && firstFailed != nil:
		out.End, out.DecidedBy = WaitFirstFailed, firstFailed.ID
	case any:
		out.End, out.DecidedBy = WaitFirstSucceeded, firstFinal.ID
	case final < len(jobs):
		return WaitOutcome{}, false
	case firstFailed != nil:
		out.End, out.DecidedBy = WaitSomeFailed, firstFailed.ID
	default:
		out.End = WaitAllSucceeded
	}
	return out, true
}

// TimedOutWait is the outcome of a wait whose timeout passed, with the jobs as they were.
func TimedOutWait(jobs []Job) WaitOutcome { return WaitOutcome{End: WaitTimedOut, Jobs: jobs} }
