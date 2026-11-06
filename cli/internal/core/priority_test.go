package core_test

import (
	"math/rand/v2"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var anyPriority = []core.Priority{"", core.PriorityLow, core.PriorityNormal, core.PriorityHigh}

// A priority ages one level for each core.PriorityAging a job waits, never down and never past
// high, so every job counts as high once it has waited twice that long.
func TestAPriorityAgesALevelPerAgingUpToHigh(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	for range 5000 {
		p := anyPriority[r.IntN(len(anyPriority))]
		a := time.Duration(r.Int64N(int64(4*core.PriorityAging))) - core.PriorityAging
		b := a + time.Duration(r.Int64N(int64(2*core.PriorityAging)))
		pa, pb := core.AgedPriority(p, a), core.AgedPriority(p, b)
		switch {
		case !pa.Valid() || !pb.Valid():
			t.Fatalf("%q aged to %q and %q", p, pa, pb)
		case pb.Level() < pa.Level():
			t.Fatalf("%q after %v is %s, after %v only %s", p, a, pa, b, pb)
		case pa.Level() < p.Level():
			t.Fatalf("%q fell to %s after %v", p, pa, a)
		case a < core.PriorityAging && a >= 0 && pa != p.OrNormal():
			t.Fatalf("%q is %s after only %v", p, pa, a)
		case b >= 2*core.PriorityAging && pb != core.PriorityHigh:
			t.Fatalf("%q is %s after %v", p, pb, b)
		}
		if want := min(p.Level()+max(int(a/core.PriorityAging), 0), 2); pa.Level() != want {
			t.Fatalf("%q after %v is %s, want level %d", p, a, pa, want)
		}
	}
}

// A job's own priority wins; with none it takes the highest of the plans it is linked to, and no
// plan it is not linked to counts; with neither it is normal.
func TestAJobsPriorityIsItsOwnOrTheHighestOfItsPlans(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	for range 5000 {
		job := core.Job{Priority: anyPriority[r.IntN(len(anyPriority))]}
		var plans []core.Plan
		want := core.Priority("")
		for n := core.PlanNumber(1); n <= 4; n++ {
			p := core.Plan{Number: n, Priority: anyPriority[r.IntN(len(anyPriority))]}
			plans = append(plans, p)
			if r.IntN(2) == 0 {
				job.Plans = append(job.Plans, n)
				if want == "" || p.Priority.OrNormal().Level() > want.Level() {
					want = p.Priority.OrNormal()
				}
			}
		}
		if job.Priority != "" {
			want = job.Priority
		}
		if got := core.EffectivePriority(job, plans); got != want.OrNormal() {
			t.Fatalf("job %+v with plans %+v: %s, want %s", job, plans, got, want.OrNormal())
		}
	}
}
