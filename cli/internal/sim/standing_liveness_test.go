package sim_test

import (
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
)

// An agent that renews its allocation until its run ends, and then dies without saying so, has its
// run taken to have ended once it has made no call for the placement's run_idle_end: the batch job
// is cancelled and no successor is asked for, however long the daemon runs on. While it calls, it
// keeps its allocation.
func TestAStandingAllocationOfAnAgentThatDiedEndsAfterRunIdleEnd(t *testing.T) {
	inStandingBubble(t, func(t *testing.T) {
		r := newStandingRig(t, strings.Replace(standingConfig, `run_idle_end = "1000h"`, `run_idle_end = "20m"`, 1))
		a, _ := r.start()
		v, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, time.Hour)
		if err != nil {
			t.Fatal(err)
		}
		ref := r.await(a, v.ID, app.AllocUntil{Kind: app.UntilRunning}).Handle.Ref
		order := alloc.RenewalOrder{Lead: 10 * time.Minute, For: time.Hour, Until: alloc.UntilRunEnd{}}
		if _, err := a.RenewAllocation(r.ctx(), standingCall.Run, v.ID, &order); err != nil {
			t.Fatal(err)
		}
		// The agent calls every ten minutes for half an hour: the allocation stays.
		for range 3 {
			r.passes(10 * time.Minute)
			if _, err := a.Allocations(standingCall.Run, false); err != nil {
				t.Fatal(err)
			}
			r.tick()
		}
		if got := view(t, a, v.ID); got.Stage() != alloc.StageRunning {
			t.Fatalf("the allocation of a run that calls is %s", got.Stage())
		}
		// Then it dies.
		r.passes(21 * time.Minute)
		r.settle("the silent run's allocation ends", func() bool { return view(t, a, v.ID).Stage() == alloc.StageEnded })
		r.settle("its batch job is cancelled", func() bool {
			_, _, phase, ok := r.cluster.Job(ref)
			return !ok || phase == apptainer.Ended
		})
		for range 6 {
			r.passes(10 * time.Minute)
			r.tick()
		}
		if n := len(r.cluster.Submitted()); n != 1 {
			t.Fatalf("%d batch jobs submitted, want the first alone: the dead run renewed: %+v", n, r.cluster.Submitted())
		}
	})
}
