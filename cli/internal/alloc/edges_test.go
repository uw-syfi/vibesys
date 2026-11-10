package alloc_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// A lead as long as the successor's time, or longer, would have every successor due for renewal as
// it starts, so the chain would always hold max_held: it is refused, and so is a longer one.
func TestALeadNotShorterThanTheSuccessorsTimeIsRefused(t *testing.T) {
	for _, lead := range []time.Duration{6 * time.Hour, 6*time.Hour + time.Nanosecond, 20 * time.Hour} {
		w := newWorld(t)
		id := w.running(gpu, 12*time.Hour)
		w.refuse(renew(id, order(lead, 6*time.Hour)), alloc.LeadNotShorterThanFor{})
		if w.get(id).Renewal != nil {
			t.Fatalf("a refused order of lead %s was kept", lead)
		}
	}
	w := newWorld(t)
	id := w.running(gpu, 12*time.Hour)
	w.accept(renew(id, order(6*time.Hour-time.Nanosecond, 6*time.Hour)))
}

// An order given while the allocation waits is kept, does nothing while it waits, and asks for the
// successor as soon as the allocation runs with no more than the lead left.
func TestARenewalOrderOnAPendingAllocationWaitsForItsStart(t *testing.T) {
	w := newWorld(t)
	id := w.ask(gpu, 4*time.Hour)
	w.took(id)
	w.accept(renew(id, order(5*time.Hour, 8*time.Hour)))
	w.pass(3 * time.Hour)
	w.do(alloc.Tick{})
	if got := w.get(id); got.RenewedBy != "" || w.s.Held("run-a", gpu) != 1 {
		t.Fatalf("a pending allocation renewed: %+v", got)
	}
	actions := w.do(alloc.Observed{ID: id, Seen: alloc.SeenRunning{StartedAt: w.now}})
	succ := w.get(id).RenewedBy
	if succ == "" || !has(actions, alloc.Submit{Spec: alloc.Spec{ID: succ, Run: "run-a", Placement: gpu, For: 8 * time.Hour}}) {
		t.Fatalf("an allocation that started with 4h left, under its 5h lead, asked for no successor: %+v", actions)
	}
	if got := w.get(succ); got.Renewal == nil || *got.Renewal != order(5*time.Hour, 8*time.Hour) {
		t.Fatalf("the successor does not carry the order: %+v", got.Renewal)
	}
}

// Ending after the jobs an allocation that holds none ends it at once, and releases it.
func TestEndingAfterJobsWithNothingQueuedEndsAtOnce(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 12*time.Hour)
	w.submit("j1", time.Hour, id)
	w.do(alloc.JobFinished{Job: "j1"})
	actions := w.accept(alloc.EndAsk{Run: "run-a", ID: id, AfterJobs: true})
	if w.ended(id).Reason != alloc.EndRequested || !has(actions, alloc.Release{ID: id, Handle: handle(id)}) {
		t.Fatalf("ended %+v, actions %+v", w.get(id).Phase, actions)
	}
}

// The fit's boundary: a job whose bound is exactly the time left, less the margin and the work
// ahead, fits and starts; one nanosecond longer does not.
func TestAJobOfExactlyTheTimeLeftFits(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 10*time.Hour)
	w.submit("j1", 2*time.Hour, id) // Runs now, and is all ahead.
	w.pass(time.Hour)
	left := 10*time.Hour - time.Hour - 5*time.Minute - 2*time.Hour
	w.refuse(job("j2", left+time.Nanosecond, id), alloc.DoesNotFit{})
	if actions := w.accept(job("j2", left, id)); len(actions) != 0 {
		t.Fatalf("a queued job asked for %v", kinds(actions))
	}
	if got := w.live(id).Queue; len(got) != 1 || got[0].Job != "j2" {
		t.Fatalf("queue %+v, want j2", got)
	}
}

// Once the shell reports a job started, only what is left of its bound is ahead of the next one.
func TestTheRunningJobCountsWhatIsLeftOfItsBound(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 10*time.Hour)
	w.submit("j1", 4*time.Hour, id)
	w.pass(3 * time.Hour)
	// Not yet reported started: all 4h are ahead, and 7h - 5m left: 3h - 5m fit, 3h does not.
	w.refuse(job("j2", 3*time.Hour, id), alloc.DoesNotFit{})
	w.do(alloc.JobStarted{Job: "j1"})
	w.pass(2 * time.Hour)
	// Started 2h ago: 2h ahead, 5h - 5m left, so 3h - 5m fits.
	w.refuse(job("j2", 3*time.Hour, id), alloc.DoesNotFit{})
	w.accept(job("j2", 3*time.Hour-5*time.Minute, id))
}

// A successor that starts before its predecessor ends runs beside it: the predecessor runs to its
// end, keeps its jobs, and a job of any goes to whichever it fits, the soonest to end first.
func TestASuccessorThatStartsEarlyRunsBesideItsPredecessor(t *testing.T) {
	w := newWorld(t)
	old := w.running(gpu, 6*time.Hour)
	w.accept(renew(old, order(2*time.Hour, 12*time.Hour)))
	w.submit("j1", 30*time.Minute, old)
	w.pass(4 * time.Hour)
	w.do(alloc.Tick{})
	succ := w.get(old).RenewedBy
	if succ == "" {
		t.Fatal("no successor with 2h left of a 2h lead")
	}
	w.took(succ)
	w.start(succ)
	if w.stage(old) != alloc.StageRunning || w.stage(succ) != alloc.StageRunning || w.s.Held("run-a", gpu) != 2 {
		t.Fatalf("old %s, successor %s, held %d", w.stage(old), w.stage(succ), w.s.Held("run-a", gpu))
	}
	w.do(alloc.JobFinished{Job: "j1"})
	// 1h fits the old one, which ends in 2h; 3h fits only the successor.
	if a := w.submit("j2", time.Hour, ""); !has(a, alloc.StartJob{Job: "j2", ID: old, Bound: time.Hour}) {
		t.Fatalf("a 1h job of any went %+v, want into %s, which ends first", a, old)
	}
	if a := w.submit("j3", 3*time.Hour, ""); !has(a, alloc.StartJob{Job: "j3", ID: succ, Bound: 3 * time.Hour}) {
		t.Fatalf("a 3h job of any went %+v, want into %s", a, succ)
	}
	if got := w.get(succ); got.RenewedBy != "" {
		t.Fatalf("the successor renewed at once: %+v", got)
	}
}

// The end of the run ends a successor that still waits, and releases it, and nothing more is
// asked for.
func TestTheRunEndingWhileTheSuccessorWaitsEndsIt(t *testing.T) {
	w := newWorld(t)
	old := w.running(gpu, 3*time.Hour)
	w.accept(renew(old, order(2*time.Hour, 6*time.Hour)))
	w.pass(time.Hour)
	w.do(alloc.Tick{})
	succ := w.get(old).RenewedBy
	w.took(succ)
	actions := w.do(alloc.RunEnded{Run: "run-a"})
	for _, id := range []alloc.ID{old, succ} {
		if w.ended(id).Reason != alloc.EndRequested || !has(actions, alloc.Release{ID: id, Handle: handle(id)}) {
			t.Fatalf("%s %+v, actions %+v", id, w.get(id).Phase, actions)
		}
	}
	w.pass(5 * time.Hour)
	if actions := w.do(alloc.Tick{}); len(actions) != 2 {
		t.Fatalf("after the run ended a tick asked for %v, want only the two releases again", kinds(actions))
	}
	if n := len(w.s.Allocations()); n != 2 {
		t.Fatalf("%d allocations after the run ended", n)
	}
}

// A due order that finds max_held reached says so, and asks for its successor at the first step
// after another allocation of the run ends, clearing the reason.
func TestARenewalBlockedByMaxHeldGoesOnceOneIsFreed(t *testing.T) {
	w := newWorld(t)
	a := w.running(gpu, 4*time.Hour)
	b := w.running(gpu, 12*time.Hour)
	w.accept(renew(a, order(time.Hour, 4*time.Hour)))
	w.pass(3 * time.Hour)
	w.do(alloc.Tick{})
	if got := w.get(a); got.RenewedBy != "" || got.RenewalSkipped != alloc.SkipMaxHeld {
		t.Fatalf("with max_held reached: %+v", got)
	}
	w.pass(10 * time.Minute)
	w.do(alloc.Tick{})
	if got := w.get(a); got.RenewalSkipped != alloc.SkipMaxHeld {
		t.Fatalf("the reason did not hold at the next tick: %+v", got)
	}
	actions := w.accept(alloc.EndAsk{Run: "run-a", ID: b})
	got := w.get(a)
	if got.RenewedBy == "" || got.RenewalSkipped != "" || !has(actions, alloc.Submit{Spec: alloc.Spec{ID: got.RenewedBy, Run: "run-a", Placement: gpu, For: 4 * time.Hour}}) {
		t.Fatalf("after b ended: %+v, actions %v", got, kinds(actions))
	}
}

// An order whose until has passed asks for no successor, and says why.
func TestAnOrderPastItsUntilMakesNoSuccessor(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 6*time.Hour)
	o := order(time.Hour, 6*time.Hour)
	o.Until = alloc.UntilTime{At: w.now.Add(5 * time.Hour)}
	w.accept(renew(id, o))
	w.pass(5 * time.Hour) // Exactly at until, and exactly at the lead.
	w.do(alloc.Tick{})
	if got := w.get(id); got.RenewedBy != "" || got.RenewalSkipped != alloc.SkipPastUntil {
		t.Fatalf("at its until: %+v", got)
	}
}

// A pending allocation seen waiting past its queue timeout ends, and is released; one that cannot
// be asked about waits on, however long.
func TestAQueueTimeoutEndsOnlyWhatTheBackendSaysStillWaits(t *testing.T) {
	w := newWorld(t)
	id := w.ask(queue, 2*time.Hour)
	w.took(id)
	w.pass(7 * time.Hour)
	w.do(unreachable(id))
	w.do(alloc.Tick{})
	if w.stage(id) != alloc.StagePending {
		t.Fatalf("an unreachable backend ended it: %+v", w.get(id).Phase)
	}
	actions := w.do(alloc.Observed{ID: id, Seen: alloc.SeenPending{Reason: "Resources"}})
	if w.ended(id).Reason != alloc.EndQueueTimeout || !has(actions, alloc.Release{ID: id, Handle: handle(id)}) {
		t.Fatalf("seen waiting past its timeout: %+v, %v", w.get(id).Phase, kinds(actions))
	}
}

// A backend that cannot be reached past the allocation's end leaves it running, and a queued job
// that no longer fits is never started in it.
func TestAnUnreachableBackendPastTheEndStartsNothingThatDoesNotFit(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 4*time.Hour)
	w.submit("j1", 3*time.Hour, id)
	w.submit("j2", 50*time.Minute, id)
	w.do(alloc.JobStarted{Job: "j1"})
	w.pass(5 * time.Hour)
	w.do(unreachable(id))
	actions := w.do(alloc.JobFinished{Job: "j1"})
	if w.stage(id) != alloc.StageRunning {
		t.Fatalf("an unreachable backend moved it to %s", w.stage(id))
	}
	if !has(actions, alloc.DropJob{Job: "j2", ID: id, Reason: alloc.DoesNotFit{ID: id, TimeLeft: -time.Hour, Bound: 50 * time.Minute, Margin: 5 * time.Minute}}) {
		t.Fatalf("a job that no longer fits: %+v", actions)
	}
}

// A job of any that no allocation fits runs as a job without one does, and so does one queued in an
// allocation that ends or no longer fits it.
func TestAJobOfAnyFallsBackToTheSchedulerWhereverItDoesNotFit(t *testing.T) {
	w := newWorld(t)
	if a := w.submit("j0", time.Hour, ""); !has(a, alloc.NotPlaced{Job: "j0"}) {
		t.Fatalf("with no allocation: %+v", a)
	}
	id := w.running(gpu, 4*time.Hour)
	if a := w.submit("j1", 5*time.Hour, ""); !has(a, alloc.NotPlaced{Job: "j1"}) {
		t.Fatalf("too long for the one there is: %+v", a)
	}
	w.submit("j2", time.Hour, "")
	w.submit("j3", time.Hour, "")
	actions := w.do(alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedOtherwise}})
	if !has(actions, alloc.LoseJob{Job: "j2", ID: id, Reason: alloc.EndExternal}) || !has(actions, alloc.NotPlaced{Job: "j3"}) {
		t.Fatalf("its allocation cancelled outside: %+v", actions)
	}
}

// The job running when its allocation reaches its time limit is lost for the time limit.
func TestAJobRunningAtTheTimeLimitIsLostForIt(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 2*time.Hour)
	w.submit("j1", time.Hour, id)
	w.do(alloc.JobStarted{Job: "j1"})
	w.pass(2 * time.Hour)
	actions := w.do(alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedByTimeLimit, At: w.now}})
	if !has(actions, alloc.LoseJob{Job: "j1", ID: id, Reason: alloc.EndTimeLimit}) {
		t.Fatalf("actions %+v", actions)
	}
	if got := w.ended(id); got.Reason != alloc.EndTimeLimit || !got.Released {
		t.Fatalf("ended %+v", got)
	}
}

// What the backend may not have done is asked for again at every tick, and no more once it says it
// did: a submission it never took, and a release it never confirmed. A late handle of an allocation
// that ended is released.
func TestTicksAskAgainUntilTheBackendAnswers(t *testing.T) {
	w := newWorld(t)
	id := w.ask(gpu, time.Hour)
	w.do(unreachable(id))
	spec := alloc.Spec{ID: id, Run: "run-a", Placement: gpu, For: time.Hour}
	if a := w.do(alloc.Tick{}); !has(a, alloc.Submit{Spec: spec}) {
		t.Fatalf("a tick did not ask again for the submission: %+v", a)
	}
	w.accept(alloc.EndAsk{Run: "run-a", ID: id})
	if a := w.do(alloc.Tick{}); len(a) != 0 {
		t.Fatalf("an ended allocation the backend never took was asked for: %+v", a)
	}
	if a := w.do(alloc.Submitted{ID: id, Handle: handle(id)}); !has(a, alloc.Release{ID: id, Handle: handle(id)}) {
		t.Fatalf("a late handle of an ended allocation was not released: %+v", a)
	}
	if a := w.do(alloc.Tick{}); !has(a, alloc.Release{ID: id, Handle: handle(id)}) {
		t.Fatalf("an unconfirmed release was not asked again: %+v", a)
	}
	w.do(alloc.Released{ID: id})
	if a := w.do(alloc.Tick{}); len(a) != 0 {
		t.Fatalf("a confirmed release was asked again: %+v", a)
	}
}

// A queued job whose allocation's end the backend moved earlier is dropped when its turn comes,
// never started; a job of any goes to the scheduler instead.
func TestAJobThatNoLongerFitsWhenItsTurnComesIsNotStarted(t *testing.T) {
	w := newWorld(t)
	id := w.running(gpu, 10*time.Hour)
	w.submit("j1", time.Hour, id)
	w.submit("j2", 4*time.Hour, id)
	w.submit("j3", 4*time.Hour, "")
	w.do(alloc.Observed{ID: id, Seen: alloc.SeenRunning{EndsAt: w.now.Add(3 * time.Hour)}})
	actions := w.do(alloc.JobFinished{Job: "j1"})
	if !has(actions, alloc.NotPlaced{Job: "j3"}) || !slicesHasType[alloc.DropJob](actions) {
		t.Fatalf("actions %+v", actions)
	}
	if l := w.live(id); l.Current != nil || len(l.Queue) != 0 {
		t.Fatalf("live %+v", l)
	}
}

func slicesHasType[T alloc.Action](actions []alloc.Action) bool {
	for _, a := range actions {
		if _, ok := a.(T); ok {
			return true
		}
	}
	return false
}

// The same log of inputs folds to the same state and the same actions, and the fold is the steps
// one after the other.
func TestFoldIsTheStepsAndIsDeterministic(t *testing.T) {
	w := newWorld(t)
	old := w.running(gpu, 3*time.Hour)
	w.accept(renew(old, order(time.Hour, 3*time.Hour)))
	w.submit("j1", time.Hour, old)
	w.submit("j2", time.Hour, "")
	w.pass(2 * time.Hour)
	w.do(alloc.Tick{})
	w.do(alloc.RunEnded{Run: "run-a"})
	s1, a1 := alloc.Fold(w.cfg, w.log)
	s2, a2 := alloc.Fold(w.cfg, w.log)
	if !equalStates(s1, w.s) || !equalStates(s1, s2) || !equalActions(a1, w.actions) || !equalActions(a1, a2) {
		t.Fatalf("the fold differs from the steps, or from itself")
	}
}
