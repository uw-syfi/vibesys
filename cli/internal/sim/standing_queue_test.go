package sim_test

import (
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
)

// Regression: the placement's slurm queue_timeout_seconds is the queue timeout of its standing
// allocations, through the daemon's production wiring: an allocation that still waits in Slurm's queue
// past it ends QUEUE_TIMEOUT and its batch job is cancelled, and one within it, or of a placement that
// sets none (a day), keeps waiting.
func TestAPendingStandingAllocationPastTheQueueTimeoutEndsAndIsCancelled(t *testing.T) {
	withTimeout := strings.Replace(standingConfig, "max_time_seconds = 7200", "max_time_seconds = 7200\nqueue_timeout_seconds = 600", 1)
	for name, c := range map[string]struct {
		config string
		wait   time.Duration
		ends   bool
	}{
		"past the placement's timeout":    {withTimeout, 11 * time.Minute, true},
		"within the placement's timeout":  {withTimeout, 9 * time.Minute, false},
		"no timeout set, a day's default": {standingConfig, 11 * time.Minute, false},
	} {
		t.Run(name, func(t *testing.T) {
			r := newStandingRig(t, c.config)
			r.cluster.Queue(1<<30, false) // the job never leaves Slurm's queue
			a, _ := r.start()
			v, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, time.Hour)
			if err != nil {
				t.Fatal(err)
			}
			r.tick()
			r.passes(c.wait)
			// Each tick is a look of the daemon at its backend, and what it finds is posted back to it
			// meanwhile: a wait is how the test reads the daemon after its turn.
			var got app.AllocationView
			for range 5000 {
				r.tick()
				var outcome app.AllocWaitOutcome
				if got, outcome, err = a.WaitAllocation(r.ctx(), v.ID, app.AllocUntil{Kind: app.UntilEnded}, time.Millisecond); err != nil {
					t.Fatal(err)
				}
				if outcome != app.AllocWaitTimedOut {
					break
				}
			}
			ended, isEnded := got.Phase.(alloc.Ended)
			if isEnded != c.ends || (c.ends && ended.Reason != alloc.EndQueueTimeout) {
				t.Fatalf("the allocation is %+v; want ended %v, by the queue timeout", got.Phase, c.ends)
			}
			if c.ends {
				// The release the core asks for lands at the backend.
				// The release is an effect the daemon posts back when done; each tick is a turn of its loop.
				for range 5000 {
					if len(r.cluster.Live()) == 0 {
						break
					}
					r.tick()
				}
				if live := r.cluster.Live(); len(live) != 0 {
					t.Errorf("batch jobs %v still live after the allocation ended", live)
				}
			}
		})
	}
}
