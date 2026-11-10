package lifecycle_test

import (
	"fmt"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// A standing allocation holds a slot of its placement for as long as it lives, and the jobs that run
// in it claim nothing more. These tests drive the machine directly and read its requests.

// slotRig is a machine of one placement with the given number of slots, past its recovery.
type slotRig struct {
	t     *testing.T
	state lifecycle.State
	reqs  []lifecycle.Request
	n     int
}

func newSlotRig(t *testing.T, capacity int) *slotRig {
	r := &slotRig{t: t}
	sc := lifecycle.Scheduling{Resources: []schedule.Resource{{ID: schedule.PlacementOf("p"), Capacity: capacity}}}
	r.state, _ = lifecycle.Step(lifecycle.RecoveringWith(sc), lifecycle.RecoveryDone{})
	return r
}

func (r *slotRig) feed(e lifecycle.Event) []lifecycle.Request {
	r.t.Helper()
	var reqs []lifecycle.Request
	r.state, reqs = lifecycle.Step(r.state, e)
	r.reqs = append(r.reqs, reqs...)
	return reqs
}

func (r *slotRig) ref() lifecycle.Ref {
	r.n++
	return lifecycle.Ref{Store: "s", Job: core.JobID(fmt.Sprintf("j%d", r.n))}
}

// admit queues a job that claims a slot of p, in an allocation or not.
func (r *slotRig) admit(allocated bool) (lifecycle.Ref, []lifecycle.Request) {
	ref := r.ref()
	work := lifecycle.Work{
		Kind: core.JobCommand, Run: "run-a", Allocated: allocated,
		Options: []schedule.Option{{Placement: "p", Claims: []schedule.Claim{{Resource: schedule.PlacementOf("p"), Amount: 1}}}},
	}
	return ref, r.feed(lifecycle.Admitted{Ref: ref, SubmittedAt: epoch, Policy: core.RerunBeforeStartOnly, Work: work})
}

func launches(reqs []lifecycle.Request) []lifecycle.LaunchRequest {
	var out []lifecycle.LaunchRequest
	for _, q := range reqs {
		if l, ok := q.(lifecycle.LaunchRequest); ok {
			out = append(out, l)
		}
	}
	return out
}

func (r *slotRig) granted(slot string) bool {
	for _, s := range r.state.Slots() {
		if s.Slot == slot {
			return s.Granted
		}
	}
	return false
}

func (r *slotRig) finish(ref lifecycle.Ref) []lifecycle.Request {
	r.t.Helper()
	r.feed(lifecycle.Launched{Ref: ref})
	r.feed(lifecycle.Finished{Ref: ref, At: epoch, Outcome: lifecycle.Outcome{End: lifecycle.CommandEnded{Attempt: core.Ended{Exec: core.ExecOutcome{Kind: core.ExecExited}}}}})
	return r.feed(lifecycle.Closed{Ref: ref})
}

func TestAJobInAnAllocationClaimsNothingOfItsOwn(t *testing.T) {
	r := newSlotRig(t, 1)
	r.feed(lifecycle.SlotWanted{Slot: "a1", Placement: "p", Run: "run-a", At: epoch})
	if !r.granted("a1") {
		t.Fatalf("the only slot of a free placement is not granted: %+v", r.state.Slots())
	}
	job, reqs := r.admit(true)
	if got := launches(reqs); len(got) != 0 {
		t.Fatalf("a job held for an allocation was launched: %+v", got)
	}
	got := launches(r.feed(lifecycle.AllocReleased{Ref: job, Placement: "p", Allocation: "ref-1"}))
	want := []lifecycle.LaunchRequest{{Ref: job, Placement: "p", Allocation: "ref-1"}}
	if !slices.EqualFunc(got, want, func(a, b lifecycle.LaunchRequest) bool {
		return a.Ref == b.Ref && a.Placement == b.Placement && a.Allocation == b.Allocation && len(a.Leases) == 0
	}) {
		t.Fatalf("released into its allocation: launches %+v, want %+v, although the allocation holds the placement's only slot", got, want)
	}
}

func TestAJobWithNoAllocationWaitsForTheSlotAnAllocationHolds(t *testing.T) {
	r := newSlotRig(t, 1)
	r.feed(lifecycle.SlotWanted{Slot: "a1", Placement: "p", Run: "run-a", At: epoch})
	plain, reqs := r.admit(false)
	if len(launches(reqs)) != 0 {
		t.Fatalf("a job started on a placement whose only slot an allocation holds")
	}
	if q := r.state.Queue(); len(q) != 1 || q[0].Ref != plain || q[0].Reason.Kind != core.WaitFull {
		t.Fatalf("the job's wait: %+v, want it waiting for a full placement", q)
	}
	got := launches(r.feed(lifecycle.SlotDropped{Slot: "a1"}))
	if len(got) != 1 || got[0].Ref != plain {
		t.Fatalf("after the allocation gave its slot back: launches %+v, want %v", got, plain)
	}
}

func TestASlotWaitsForARunningJobAndTakesTheSlotItLeaves(t *testing.T) {
	r := newSlotRig(t, 1)
	job, reqs := r.admit(false)
	if len(launches(reqs)) != 1 {
		t.Fatalf("the job did not start: %+v", reqs)
	}
	r.feed(lifecycle.SlotWanted{Slot: "a1", Placement: "p", Run: "run-a", At: epoch})
	if r.granted("a1") {
		t.Fatal("a slot was granted on a placement whose only slot a job holds")
	}
	r.finish(job)
	if !r.granted("a1") {
		t.Fatalf("the slot is not granted once the job left: %+v", r.state.Slots())
	}
}

func TestAJobReleasedWithNoAllocationRunsAsAnyOtherJobDoes(t *testing.T) {
	r := newSlotRig(t, 1)
	r.feed(lifecycle.SlotWanted{Slot: "a1", Placement: "p", Run: "run-a", At: epoch})
	job, _ := r.admit(true)
	if got := launches(r.feed(lifecycle.AllocReleased{Ref: job})); len(got) != 0 {
		t.Fatalf("a job released to run as any other does started with no slot free: %+v", got)
	}
	got := launches(r.feed(lifecycle.SlotDropped{Slot: "a1"}))
	if len(got) != 1 || got[0].Ref != job || got[0].Allocation != "" {
		t.Fatalf("after the slot was freed: %+v, want job %v with no allocation", got, job)
	}
}

func TestARestoredSlotIsHeldAtOnce(t *testing.T) {
	r := newSlotRig(t, 1)
	r.feed(lifecycle.SlotWanted{Slot: "a1", Placement: "p", Run: "run-a", At: epoch, Restored: true})
	if !r.granted("a1") {
		t.Fatal("a slot an allocation held before a restart is not held again")
	}
	r.feed(lifecycle.SlotWanted{Slot: "a1", Placement: "p", Run: "run-a", At: epoch})
	if n := len(r.state.Slots()); n != 1 {
		t.Fatalf("wanting a slot twice made %d slots", n)
	}
}

// Over random streams of slots and jobs, the slots granted and the claims of the jobs that run never
// add up to more than the placement has, and a job in an allocation never holds a claim.
func TestSlotsAndJobsNeverOverfillAPlacement(t *testing.T) {
	for seed := range uint64(60) {
		rnd := newRand(seed)
		capacity := 1 + rnd.IntN(3)
		r := newSlotRig(t, capacity)
		slots, jobs := 0, []lifecycle.Ref{}
		running := map[lifecycle.Ref]bool{}
		inAlloc := map[lifecycle.Ref]bool{}
		for range 80 {
			switch rnd.IntN(5) {
			case 0:
				slots++
				r.feed(lifecycle.SlotWanted{Slot: fmt.Sprintf("a%d", slots), Placement: "p", Run: "run-a", At: epoch})
			case 1:
				if slots > 0 {
					r.feed(lifecycle.SlotDropped{Slot: fmt.Sprintf("a%d", 1+rnd.IntN(slots))})
				}
			case 2, 3:
				allocated := rnd.IntN(2) == 0
				ref, reqs := r.admit(allocated)
				jobs = append(jobs, ref)
				for _, l := range launches(reqs) {
					running[l.Ref] = true
				}
				if allocated && rnd.IntN(4) != 0 {
					in := rnd.IntN(2) == 0
					placement := ""
					if in {
						placement = "p"
					}
					inAlloc[ref] = in
					for _, l := range launches(r.feed(lifecycle.AllocReleased{Ref: ref, Placement: placement, Allocation: "ref"})) {
						running[l.Ref] = true
					}
				}
			case 4:
				for _, ref := range jobs {
					if running[ref] {
						delete(running, ref)
						for _, l := range launches(r.finish(ref)) {
							running[l.Ref] = true
						}
						break
					}
				}
			}
			used := 0
			for _, s := range r.state.Slots() {
				if s.Granted {
					used++
				}
			}
			for ref := range running {
				if !inAlloc[ref] {
					used++
				}
			}
			if used > capacity {
				t.Fatalf("seed %d: %d claims on a placement of %d slots: slots %+v, running %v", seed, used, capacity, r.state.Slots(), running)
			}
		}
	}
}
