package sim_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
)

// A job whose container's processes in a standing allocation could not be confirmed killed ends
// the allocation, EXTERNAL, before the job queued behind it starts there: the batch job is
// cancelled, and the next job never starts beside what was left. The failed kill is the
// supervisor's to learn, in the supervisor's own engine, and reaches the daemon in the job's
// outcome. Before, the kill's failure was only logged and the next job ran beside the processes.
func TestAJobThatLeftProcessesInItsAllocationEndsItBeforeTheNextJobStarts(t *testing.T) {
	inStandingBubble(t, func(t *testing.T) {
		r := newStandingRig(t, standingConfig)
		a, _ := r.start()
		v, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, time.Hour)
		if err != nil {
			t.Fatal(err)
		}
		ref := r.await(a, v.ID, app.AllocUntil{Kind: app.UntilRunning}).Handle.Ref
		first := r.jobIn(a, string(v.ID), "mark", "left-first", ";", "hold", "g-left", ";", "exit", "0")
		if !r.gates.Reached(r.ctx(), "left-first") {
			t.Fatal("the first job did not start")
		}
		next := r.jobIn(a, string(v.ID), "print", "next", ";", "exit", "0")
		// From now on no kill on the node is confirmed.
		r.cluster.SetFaults(func(op string) error {
			if op == "signal" {
				return unreachable()
			}
			return nil
		})
		started := len(r.cluster.StartedIn(ref))
		r.gates.Release("g-left")
		r.finalJob(a, first.ID)
		r.settle("the allocation ends", func() bool { return view(t, a, v.ID).Stage() == alloc.StageEnded })
		if end := view(t, a, v.ID).Phase.(alloc.Ended); end.Reason != alloc.EndExternal {
			t.Fatalf("the allocation ended %s, want external", end.Reason)
		}
		r.settle("its batch job is cancelled", func() bool {
			_, _, phase, ok := r.cluster.Job(ref)
			return !ok || phase == apptainer.Ended
		})
		r.finalJob(a, next.ID)
		if n := len(r.cluster.StartedIn(ref)); n != started {
			t.Fatalf("%d containers started in %s after the first job's, want none: the next job ran beside its processes", n-started, ref)
		}
	})
}
