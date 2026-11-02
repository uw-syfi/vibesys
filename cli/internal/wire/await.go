package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// Await decides whether a wait on the given jobs is over, from the states the daemon reported.
// It is the rule of lifecycle.Await over the contract's messages, so the client, which works in
// those, does not hold a second copy of it. A state the contract names and the domain does not is
// read as not final, so a wait never ends on a state it cannot read.
func Await(jobs []*cliv1.Job, any bool) (done, succeeded bool) {
	states := make([]core.JobState, 0, len(jobs))
	for _, j := range jobs {
		s, err := ToJobState("state", j.GetState())
		if err != nil {
			s = core.JobQueued
		}
		states = append(states, s)
	}
	return lifecycle.Await(states, any)
}
