package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var waitEnds = map[core.WaitEnd]cliv1.WaitOutcomeKind{
	core.WaitAllSucceeded:   cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_ALL_SUCCEEDED,
	core.WaitSomeFailed:     cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_SOME_FAILED,
	core.WaitFirstSucceeded: cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_FIRST_SUCCEEDED,
	core.WaitFirstFailed:    cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_FIRST_FAILED,
	core.WaitTimedOut:       cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_TIMED_OUT,
}

// WaitOutcome returns the contract's message for how a wait ended. An end this table lacks is
// UNSPECIFIED, which every client reads as no answer, never as success.
func WaitOutcome(o core.WaitOutcome) *cliv1.WaitOutcome {
	return &cliv1.WaitOutcome{Kind: waitEnds[o.End], DecidedBy: string(o.DecidedBy), Jobs: Jobs(o.Jobs)}
}

// ToWaitFor returns what a Wait request waits for. It fails with a failure of kind invalid input
// for no job, an empty job id, or a timeout that is not a valid positive duration.
func ToWaitFor(r *cliv1.WaitRequest) (core.WaitFor, error) {
	out := core.WaitFor{Any: r.GetAny()}
	if len(r.GetJobIds()) == 0 {
		return core.WaitFor{}, invalid("job_ids: name at least one job to wait for")
	}
	for i, id := range r.GetJobIds() {
		if id == "" {
			return core.WaitFor{}, invalid("job_ids[%d]: empty", i)
		}
		out.IDs = append(out.IDs, core.JobID(id))
	}
	if t := r.GetTimeout(); t != nil {
		if err := t.CheckValid(); err != nil {
			return core.WaitFor{}, invalid("timeout: %v", err)
		}
		if t.AsDuration() <= 0 {
			return core.WaitFor{}, invalid("timeout: %v is not positive", t.AsDuration())
		}
		out.Timeout = t.AsDuration()
	}
	return out, nil
}
