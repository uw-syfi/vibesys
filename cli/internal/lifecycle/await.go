package lifecycle

import "github.com/uw-syfi/vibesys/cli/internal/core"

// Await decides whether a wait on jobs is over, from their current states. A wait for all jobs is
// over when every one is final. With any, it is over when at least one is. Succeeded says every
// job that is final succeeded: a failed, cancelled or lost job is not a success. A wait on no jobs
// is over at once and succeeded, since nothing it waits for failed; callers reject it as input
// before they ask.
//
// A wait that times out is not decided here: the timeout is the client closing its watch, and it
// never cancels a job.
func Await(states []core.JobState, any bool) (done, succeeded bool) {
	final, ok := 0, true
	for _, s := range states {
		if !s.Final() {
			continue
		}
		final++
		if s != core.JobSucceeded {
			ok = false
		}
	}
	if any {
		return final > 0 || len(states) == 0, ok
	}
	return final == len(states), ok
}
