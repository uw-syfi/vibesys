package lifecycle_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fairshare"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
)

// These tests run the machine with more than one slot: the same generated event sequences as the
// tests of one job at a time, with each job's claims checked against the resources after every
// event.

const gpu schedule.ResourceID = "gpu:0"

// newScheduledWorld is a world whose machine has slots of the default placement, of two more
// placements and one GPU, and whose jobs are checks (a shared slot), commands (a shared slot,
// sometimes the GPU) and measures (the placement alone), from three runs. Some may run on either
// of the two placements, and the scheduler chooses which when it starts them.
func newScheduledWorld(t *testing.T, seed uint64, stores int) *world {
	w := newWorld(t, seed, stores)
	policy := fifo.New()
	if seed%2 == 1 {
		policy = fairshare.New()
	}
	w.scheduled = true
	w.scheduling = lifecycle.Scheduling{
		Resources: []schedule.Resource{
			{ID: schedule.DefaultPlacement, Capacity: 1 + int(seed%4)}, {ID: gpu, Capacity: 1},
			{ID: schedule.PlacementOf("pa"), Capacity: 1 + int(seed%2)}, {ID: schedule.PlacementOf("pb"), Capacity: 1},
		},
		Policy: policy,
	}
	w.work = map[lifecycle.Ref]lifecycle.Work{}
	w.workOf = randomWork
	w.state, _ = lifecycle.Step(lifecycle.RecoveringWith(w.scheduling), lifecycle.RecoveryDone{})
	return w
}

func randomWork(r *rand.Rand) lifecycle.Work {
	run := core.RunID(fmt.Sprintf("run-%c", 'a'+r.IntN(3)))
	if r.IntN(3) == 0 {
		// A docker job that either placement fits.
		kind := []core.JobKind{core.JobCheck, core.JobMeasure, core.JobCommand}[r.IntN(3)]
		measure := kind == core.JobMeasure
		var options []schedule.Option
		for _, p := range []string{"pa", "pb"} {
			claims := []schedule.Claim{{Resource: schedule.PlacementOf(p), Amount: 1, Exclusive: measure}}
			if kind == core.JobCommand && r.IntN(2) == 0 {
				claims = append(claims, schedule.Claim{Resource: gpu, Amount: 1})
			}
			options = append(options, schedule.Option{Placement: p, Claims: claims})
		}
		return lifecycle.Work{Kind: kind, Run: run, Options: options}
	}
	switch r.IntN(4) {
	case 0:
		return lifecycle.Work{Kind: core.JobMeasure, Run: run, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: true})}
	case 1:
		return lifecycle.Work{Kind: core.JobCommand, Run: run, Options: schedule.Only(
			schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1}, schedule.Claim{Resource: gpu, Amount: 1},
		)}
	}
	return lifecycle.Work{Kind: core.JobCheck, Run: run, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1})}
}

// overClaimed says how the jobs that hold the machine break a resource's capacity or an exclusive
// claim. A job with no work claims every resource alone.
func (w *world) overClaimed(active []lifecycle.Ref) string {
	resources := w.scheduling.Resources
	if !w.scheduled {
		resources = lifecycle.OneAtATime().Resources
	}
	capacity := map[schedule.ResourceID]int{}
	for _, r := range resources {
		capacity[r.ID] = r.Capacity
	}
	used := map[schedule.ResourceID]int{}
	holders := map[schedule.ResourceID][]lifecycle.Ref{}
	alone := map[schedule.ResourceID]bool{}
	for _, ref := range active {
		for _, c := range w.heldBy(ref) {
			used[c.Resource] += c.Amount
			holders[c.Resource] = append(holders[c.Resource], ref)
			alone[c.Resource] = alone[c.Resource] || c.Exclusive
		}
	}
	for r, n := range used {
		switch {
		case n > capacity[r]:
			return fmt.Sprintf("%s holds %d of capacity %d: %v", r, n, capacity[r], holders[r])
		case alone[r] && len(holders[r]) > 1:
			return fmt.Sprintf("%s is held exclusively and shared by %v", r, holders[r])
		}
	}
	return ""
}

// heldBy is what a job holds while it holds the machine, as the model knows it: the claims of the
// placement the machine launched it on, or of the one place it may run, or every resource alone
// for a job with no work. A job of several places that the machine holds without having launched it
// holds the most any of them claims of each resource.
func (w *world) heldBy(ref lifecycle.Ref) []schedule.Claim {
	work := w.work[ref]
	if len(work.Options) == 0 {
		resources := w.scheduling.Resources
		if !w.scheduled {
			resources = lifecycle.OneAtATime().Resources
		}
		var claims []schedule.Claim
		for _, r := range resources {
			claims = append(claims, schedule.Claim{Resource: r.ID, Amount: 1, Exclusive: true})
		}
		return claims
	}
	if p, ok := w.on[ref]; ok {
		i := slices.IndexFunc(work.Options, func(o schedule.Option) bool { return o.Placement == p })
		return work.Options[i].Claims
	}
	if len(work.Options) == 1 {
		return work.Options[0].Claims
	}
	var out []schedule.Claim
	for _, o := range work.Options {
		for _, c := range o.Claims {
			i := slices.IndexFunc(out, func(h schedule.Claim) bool { return h.Resource == c.Resource })
			if i < 0 {
				out = append(out, c)
				continue
			}
			out[i].Amount, out[i].Exclusive = max(out[i].Amount, c.Amount), out[i].Exclusive || c.Exclusive
		}
	}
	return out
}

// With more than one slot the machine runs jobs beside each other: some sequence of events has more
// than one command running at once, or the tests above hold of nothing.
func TestWithSeveralSlotsJobsRunBesideEachOther(t *testing.T) {
	most := 0
	for seed := range uint64(100) {
		w := newScheduledWorld(t, seed, 2)
		for range 80 {
			w.churn(1, true)
			running := 0
			for _, h := range w.state.Held() {
				if h.Phase == lifecycle.PhaseRunning {
					running++
				}
			}
			most = max(most, running)
		}
		w.drain()
	}
	if most < 3 {
		t.Fatalf("at most %d jobs ran at once in any sequence", most)
	}
}

func TestWithSeveralSlotsRecoveryAfterACrashAtAnyPointSettlesEveryJob(t *testing.T) {
	for seed := range uint64(300) {
		w := newScheduledWorld(t, seed, 1+int(seed%3))
		w.churn(10+w.r.IntN(60), false)
		for _, ref := range w.order {
			if s := w.sup[ref]; s != nil && s.alive && w.r.IntN(4) == 0 {
				s.alive = false
			}
		}
		w.crash()
		w.recoverAll(w.r.IntN(len(w.order) + 1))
		w.crash()
		w.recoverAll(len(w.order))
		w.feed(lifecycle.RecoveryDone{})
		w.drain()
	}
}

// The scheduling of a named example: three checks share the placement's four slots, a measure
// submitted next waits for the placement to be free, and a check after it, which a slot is free
// for, waits behind the measure.
func TestAMeasureWaitsForThePlacementAndKeepsItsTurn(t *testing.T) {
	sc := lifecycle.Scheduling{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 4}}, Policy: fifo.New()}
	s, _ := lifecycle.Step(lifecycle.RecoveringWith(sc), lifecycle.RecoveryDone{})
	store := lifecycle.StoreIDOf("/repo", "task")
	ref := func(n int) lifecycle.Ref { return lifecycle.Ref{Store: store, Job: core.JobID(fmt.Sprintf("j%d", n))} }
	check := func(run core.RunID) lifecycle.Work {
		return lifecycle.Work{Kind: core.JobCheck, Run: run, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1})}
	}
	measure := lifecycle.Work{Kind: core.JobMeasure, Run: "run-b", Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: true})}
	var launched []lifecycle.Ref
	feed := func(e lifecycle.Event) {
		t.Helper()
		var reqs []lifecycle.Request
		s, reqs = lifecycle.Step(s, e)
		for _, r := range reqs {
			if l, ok := r.(lifecycle.LaunchRequest); ok {
				launched = append(launched, l.Ref)
			}
		}
	}
	at := epoch
	admit := func(n int, w lifecycle.Work) {
		at = at.Add(time.Second)
		feed(lifecycle.Admitted{Ref: ref(n), SubmittedAt: at, Work: w})
	}
	for n := 1; n <= 3; n++ {
		admit(n, check("run-a"))
	}
	if !slices.Equal(launched, []lifecycle.Ref{ref(1), ref(2), ref(3)}) {
		t.Fatalf("launched %v, want the three checks", launched)
	}
	admit(4, measure)
	admit(5, check("run-a"))
	if len(launched) != 3 {
		t.Fatalf("launched %v beside the checks", launched[3:])
	}
	queue := s.Queue()
	if len(queue) != 2 || queue[0].Ref != ref(4) || queue[0].Reason.Kind != core.WaitInUse || len(queue[0].Blockers) != 3 {
		t.Fatalf("the measure waits as %+v, want in use by the three checks", queue)
	}
	if queue[1].Ref != ref(5) || queue[1].Reason.Kind != core.WaitBehind || queue[1].Blockers[0].Ref != ref(4) || queue[1].Blockers[0].Work.Kind != core.JobMeasure {
		t.Fatalf("the late check waits as %+v, want behind the measure", queue[1])
	}
	if w := s.Waiting(); len(w) != 0 {
		t.Fatalf("jobs that wait their turn are reported as trouble: %+v", w)
	}
	// The checks end one by one; the measure starts only once the last end is recorded.
	end := func(n int) {
		feed(lifecycle.Launched{Ref: ref(n)})
		feed(lifecycle.Finished{Ref: ref(n), Outcome: lifecycle.Outcome{End: lifecycle.CommandEnded{Attempt: core.Ended{Exec: core.ExecOutcome{Kind: core.ExecExited}}}}, At: at})
		feed(lifecycle.Closed{Ref: ref(n)})
	}
	end(1)
	end(2)
	if len(launched) != 3 {
		t.Fatalf("launched %v while a check holds a slot", launched[3:])
	}
	feed(lifecycle.Launched{Ref: ref(3)})
	feed(lifecycle.Finished{Ref: ref(3), Outcome: lifecycle.Outcome{End: lifecycle.CommandEnded{Attempt: core.Ended{Exec: core.ExecOutcome{Kind: core.ExecExited}}}}, At: at})
	if len(launched) != 3 {
		t.Fatalf("launched %v before the last check's end was recorded", launched[3:])
	}
	feed(lifecycle.Closed{Ref: ref(3)})
	if !slices.Equal(launched[3:], []lifecycle.Ref{ref(4)}) {
		t.Fatalf("launched %v after the checks, want the measure alone", launched[3:])
	}
	queue = s.Queue()
	if len(queue) != 1 || queue[0].Ref != ref(5) || queue[0].Reason.Kind != core.WaitHeldExclusively || queue[0].Blockers[0].Work.Run != "run-b" {
		t.Fatalf("the late check waits as %+v, want held exclusively by the measure", queue)
	}
}

// A restart adopts the jobs that run with their claims, even past the capacity it now has, and
// starts nothing beside them that does not fit. A job in doubt whose work is unknown holds every
// resource for its limit.
func TestRecoveryAdoptsRunningJobsWithTheirClaims(t *testing.T) {
	sc := lifecycle.Scheduling{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 2}}}
	store := lifecycle.StoreIDOf("/repo", "task")
	ref := func(n int) lifecycle.Ref { return lifecycle.Ref{Store: store, Job: core.JobID(fmt.Sprintf("j%d", n))} }
	shared := lifecycle.Work{Kind: core.JobCheck, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1})}
	alone := lifecycle.Work{Kind: core.JobMeasure, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: true})}
	alive := lifecycle.RunAlive{Phase: lifecycle.RunRunning, Limit: time.Hour}

	for name, tc := range map[string]struct {
		running []lifecycle.Work
		starts  bool
	}{
		"a shared job leaves a slot":     {running: []lifecycle.Work{shared}, starts: true},
		"two shared jobs fill the slots": {running: []lifecycle.Work{shared, shared}},
		"a measure holds the placement":  {running: []lifecycle.Work{alone}},
		"unknown work holds everything":  {running: []lifecycle.Work{{}}},
	} {
		t.Run(name, func(t *testing.T) {
			s := lifecycle.RecoveringWith(sc)
			var reqs []lifecycle.Request
			for i, w := range tc.running {
				finding := lifecycle.Finding(lifecycle.JobIn{State: core.JobRunning, SubmittedAt: epoch, Work: w})
				presence := lifecycle.Presence(alive)
				if len(w.Options) == 0 {
					finding, presence = lifecycle.StoreAway{}, lifecycle.RunUnknown{Limit: time.Hour}
				}
				s, _ = lifecycle.Step(s, lifecycle.Recovered{Ref: ref(i + 1), Finding: finding, Supervisor: presence, At: epoch})
			}
			// The queued job is of another store, which a job in doubt does not hold back.
			other := lifecycle.Ref{Store: lifecycle.StoreIDOf("/repo", "other"), Job: "j9"}
			s, _ = lifecycle.Step(s, lifecycle.Recovered{
				Ref: other, Finding: lifecycle.JobIn{State: core.JobQueued, SubmittedAt: epoch.Add(time.Second), Work: shared}, Supervisor: lifecycle.RunNone{}, At: epoch,
			})
			s, reqs = lifecycle.Step(s, lifecycle.RecoveryDone{})
			started := slices.ContainsFunc(reqs, func(r lifecycle.Request) bool { _, ok := r.(lifecycle.LaunchRequest); return ok })
			if started != tc.starts {
				t.Fatalf("the queued check started: %v, want %v (%+v)", started, tc.starts, s.Held())
			}
		})
	}
}

// A job whose claims no state of the resources can grant never starts, and a wait on it is told why
// rather than left waiting; the jobs after it go on.
func TestAJobThatCanNeverStartSaysSo(t *testing.T) {
	s, _ := lifecycle.Step(lifecycle.RecoveringWith(lifecycle.OneAtATime()), lifecycle.RecoveryDone{})
	store := lifecycle.StoreIDOf("/repo", "task")
	never := lifecycle.Ref{Store: store, Job: "j1"}
	fine := lifecycle.Ref{Store: store, Job: "j2"}
	s, reqs := lifecycle.Step(s, lifecycle.Admitted{Ref: never, SubmittedAt: epoch, Work: lifecycle.Work{Options: schedule.Only(schedule.Claim{Resource: gpu, Amount: 1})}})
	if len(reqs) != 0 {
		t.Fatalf("a job claiming a resource there is none of asked for %+v", reqs)
	}
	if w := s.Waiting(); len(w) != 1 || w[0].Ref != never || w[0].Failure.Message != `it can never start: no resource "gpu:0"` {
		t.Fatalf("the job waits as %+v", w)
	}
	_, reqs = lifecycle.Step(s, lifecycle.Admitted{Ref: fine, SubmittedAt: epoch.Add(time.Second)})
	if len(reqs) != 1 || !reflect.DeepEqual(reqs[0], lifecycle.LaunchRequest{Ref: fine, Leases: []schedule.ResourceID{schedule.DefaultPlacement}}) {
		t.Fatalf("the job after it asked for %+v", reqs)
	}
}
