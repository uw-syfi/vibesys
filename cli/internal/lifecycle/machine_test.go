package lifecycle_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// Every test here draws from a seeded generator, so a failure repeats exactly from its seed, and
// time is a number the test steps: nothing reads a clock.

func newRand(seed uint64) *rand.Rand { return rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)) }

var epoch = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)

// supervisor is the model of one job's supervisor.
type supervisor struct {
	alive        bool
	cancelAsked  bool
	cancelReason string
	finished     bool
}

// world is a model of everything the machine's shell owns: the store's job states, the
// supervisors, and the requests the machine has made and nobody has answered yet. Its answers are
// the ones the real shell may give, chosen by the generator.
type world struct {
	t        *testing.T
	r        *rand.Rand
	state    lifecycle.State
	now      time.Time
	store    map[lifecycle.Ref]core.JobState
	at       map[lifecycle.Ref]time.Time
	order    []lifecycle.Ref
	sup      map[lifecycle.Ref]*supervisor
	dirs     map[lifecycle.Ref]bool // run directories that exist
	pending  []lifecycle.Request
	launches []lifecycle.Ref
	moves    map[lifecycle.Ref]int
	// stores is how many stores jobs are spread over, so FIFO is checked across stores.
	stores int
	next   map[lifecycle.StoreID]int
}

func newWorld(t *testing.T, seed uint64, stores int) *world {
	return &world{
		t: t, r: newRand(seed), now: epoch,
		store: map[lifecycle.Ref]core.JobState{}, at: map[lifecycle.Ref]time.Time{},
		sup: map[lifecycle.Ref]*supervisor{}, dirs: map[lifecycle.Ref]bool{},
		moves: map[lifecycle.Ref]int{}, stores: stores, next: map[lifecycle.StoreID]int{},
	}
}

func (w *world) tick() time.Time {
	w.now = w.now.Add(time.Second)
	return w.now
}

func (w *world) fail(format string, args ...any) {
	w.t.Helper()
	w.t.Fatalf(format, args...)
}

// feed gives the machine an event and checks the machine and every request it made.
func (w *world) feed(e lifecycle.Event) {
	w.t.Helper()
	var reqs []lifecycle.Request
	w.state, reqs = lifecycle.Step(w.state, e)
	for _, req := range reqs {
		w.checkRequest(req)
		w.pending = append(w.pending, req)
	}
	w.checkState(e)
}

func (w *world) checkState(after lifecycle.Event) {
	w.t.Helper()
	active := 0
	for _, h := range w.state.Held() {
		if h.Phase == lifecycle.PhaseLaunching || h.Phase == lifecycle.PhaseRunning || h.Phase == lifecycle.PhaseSettling {
			active++
		}
	}
	if active > 1 {
		w.fail("after %#v: %d jobs hold the machine, want at most 1: %+v", after, active, w.state.Held())
	}
	if stranded := w.state.Stranded(); len(stranded) > 0 {
		w.fail("after %#v: queued jobs %v are stranded: nothing holds the machine and nothing will start them", after, stranded)
	}
}

// checkRequest holds each request to what the model's store and supervisors allow.
func (w *world) checkRequest(req lifecycle.Request) {
	w.t.Helper()
	switch req := req.(type) {
	case lifecycle.LaunchRequest:
		if got := w.store[req.Ref]; got != core.JobQueued {
			w.fail("launch of %v, which is %q in its store", req.Ref, got)
		}
		w.launches = append(w.launches, req.Ref)
	case lifecycle.Adopt:
		if got := w.store[req.Ref]; got != core.JobRunning {
			w.fail("adopt of %v, which is %q in its store", req.Ref, got)
		}
		if s := w.sup[req.Ref]; s == nil || !s.alive {
			w.fail("adopt of %v, whose supervisor is not alive", req.Ref)
		}
	case lifecycle.Move:
		if t := req.Settlement.Transition; t.Job != req.Ref.Job || !t.To.Final() {
			w.fail("move of %v carries the transition %+v, which is not a final move of that job", req.Ref, t)
		}
	case lifecycle.SignalCancel:
		if got := w.store[req.Ref]; got != core.JobRunning {
			w.fail("cancel signal for %v, which is %q in its store", req.Ref, got)
		}
	case lifecycle.Discard:
		if got := w.store[req.Ref]; got == core.JobRunning {
			w.fail("discard of the run of %v, which is running", req.Ref)
		}
	}
}

func (w *world) submit() {
	store := lifecycle.StoreIDOf("/repo", fmt.Sprintf("task%d", w.r.IntN(w.stores)))
	w.next[store]++
	ref := lifecycle.Ref{Store: store, Job: core.JobID(fmt.Sprintf("j%d", w.next[store]))}
	w.store[ref] = core.JobQueued
	w.at[ref] = w.tick()
	w.order = append(w.order, ref)
	w.feed(lifecycle.Admitted{Ref: ref, SubmittedAt: w.at[ref]})
}

func (w *world) refs(pick func(lifecycle.Ref) bool) []lifecycle.Ref {
	var out []lifecycle.Ref
	for _, ref := range w.order {
		if pick(ref) {
			out = append(out, ref)
		}
	}
	return out
}

func (w *world) any(refs []lifecycle.Ref) (lifecycle.Ref, bool) {
	if len(refs) == 0 {
		return lifecycle.Ref{}, false
	}
	return refs[w.r.IntN(len(refs))], true
}

func (w *world) userCancel() {
	ref, ok := w.any(w.order)
	if !ok {
		return
	}
	w.feed(lifecycle.CancelAsked{Ref: ref, Reason: fmt.Sprintf("reason %d", w.r.IntN(3)), At: w.tick()})
}

// answer gives one pending request the answer the shell may give it.
func (w *world) answer(healthy bool) bool {
	if len(w.pending) == 0 {
		return false
	}
	i := w.r.IntN(len(w.pending))
	req := w.pending[i]
	w.pending = slices.Delete(w.pending, i, i+1)
	switch req := req.(type) {
	case lifecycle.LaunchRequest:
		if !healthy && w.r.IntN(4) == 0 {
			w.feed(lifecycle.LaunchFailed{Ref: req.Ref, Failure: core.Failure{Kind: core.FailureUnavailable, Message: "no environment"}, At: w.tick()})
			return true
		}
		w.store[req.Ref] = core.JobRunning
		w.dirs[req.Ref] = true
		w.sup[req.Ref] = &supervisor{alive: true}
		w.feed(lifecycle.Launched{Ref: req.Ref})
	case lifecycle.Adopt:
		// The follow ends later, when the supervisor does.
	case lifecycle.SignalCancel:
		if s := w.sup[req.Ref]; s != nil && s.alive {
			if !s.cancelAsked {
				s.cancelAsked, s.cancelReason = true, req.Reason
			}
		}
	case lifecycle.Move:
		// The store applies the transition only to a job still in the state it moves from, and a
		// request the machine makes twice would find it moved.
		t := req.Settlement.Transition
		job := core.Job{ID: t.Job, State: w.store[req.Ref], Spec: core.JobSpec{Settings: core.CommandSettings{}}}
		if _, err := job.Apply(t); err != nil {
			w.fail("move of %v from %q: core refuses the transition %+v: %v", req.Ref, w.store[req.Ref], t, err)
		}
		w.moves[req.Ref]++
		if w.moves[req.Ref] > 1 {
			w.fail("job %v is moved to a final state twice", req.Ref)
		}
		w.store[req.Ref] = t.To
		if req.Release {
			delete(w.sup, req.Ref)
			delete(w.dirs, req.Ref)
		}
		w.feed(lifecycle.Closed{Ref: req.Ref})
	case lifecycle.Discard:
		delete(w.sup, req.Ref)
		delete(w.dirs, req.Ref)
		if w.store[req.Ref] == core.JobQueued {
			w.feed(lifecycle.Discarded{Ref: req.Ref})
		} else {
			w.feed(lifecycle.Closed{Ref: req.Ref})
		}
	}
	return true
}

// running lists the refs whose supervisor is alive and which the machine is following: those the
// store has running and the machine holds as running.
func (w *world) following() []lifecycle.Ref {
	held := map[lifecycle.Ref]bool{}
	for _, h := range w.state.Held() {
		if h.Phase == lifecycle.PhaseRunning {
			held[h.Ref] = true
		}
	}
	return w.refs(func(ref lifecycle.Ref) bool {
		s := w.sup[ref]
		return s != nil && s.alive && held[ref] && w.store[ref] == core.JobRunning
	})
}

// supervisorEnds is the end of a following supervisor: it reports an outcome, or dies.
func (w *world) supervisorEnds(healthy bool) bool {
	ref, ok := w.any(w.following())
	if !ok {
		return false
	}
	s := w.sup[ref]
	if !healthy && w.r.IntN(5) == 0 {
		s.alive = false
		w.feed(lifecycle.SupervisorLost{Ref: ref, At: w.tick()})
		return true
	}
	var o lifecycle.Outcome
	switch {
	case s.cancelAsked && w.r.IntN(4) != 0:
		o = lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	case w.r.IntN(6) == 0:
		o = lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecTimedOut}}
	case w.r.IntN(8) == 0:
		o = lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecUnknown}}
	case w.r.IntN(8) == 0:
		o = lifecycle.Outcome{NotAttempted: &core.Failure{Kind: core.FailureUnavailable, Message: "no docker"}}
	default:
		o = lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecExited, ExitCode: w.r.IntN(3)}}
	}
	o.CancelAsked, o.CancelReason = s.cancelAsked, s.cancelReason
	s.finished = true
	w.feed(lifecycle.Finished{Ref: ref, Outcome: o, At: w.tick()})
	return true
}

// drain answers everything well until nothing is left to do, then checks every job ended.
func (w *world) drain() {
	w.t.Helper()
	for steps := 0; ; steps++ {
		if steps > 10_000 {
			w.fail("the machine does not settle: held %+v, pending %d", w.state.Held(), len(w.pending))
		}
		if w.answer(true) || w.supervisorEnds(true) {
			continue
		}
		break
	}
	if held := w.state.Held(); len(held) != 0 {
		w.fail("jobs are held with nothing left to answer: %+v (pending %d, store %v)", held, len(w.pending), w.store)
	}
	for _, ref := range w.order {
		if st := w.store[ref]; !st.Final() {
			w.fail("job %v is %q after the machine settled", ref, st)
		}
	}
}

func (w *world) churn(steps int, healthy bool) {
	for range steps {
		switch w.r.IntN(7) {
		case 0, 1:
			w.submit()
		case 2:
			w.userCancel()
		case 3, 4:
			w.answer(healthy)
		case 5, 6:
			w.supervisorEnds(healthy)
		}
	}
}

func TestEveryEventSequenceKeepsTheMachineSafeAndMakesProgress(t *testing.T) {
	for seed := range uint64(300) {
		w := newWorld(t, seed, 1+int(seed%3))
		w.churn(80, false)
		w.drain()
		checkFIFO(t, w)
	}
}

// checkFIFO holds the launch order to submission order among jobs that were waiting together: a
// job launched after another was submitted before it, unless it was not yet submitted when the
// other launched. The model submits at the same pace it launches, so the check is: no job is
// launched while an older queued job is still waiting and was launchable.
func checkFIFO(t *testing.T, w *world) {
	t.Helper()
	// A job's launch position among launches never passes a job submitted earlier that was
	// already waiting at that time. Waiting is approximated by "submitted before this launch".
	launchedAt := map[lifecycle.Ref]int{}
	for i, ref := range w.launches {
		launchedAt[ref] = i
	}
	for i, ref := range w.launches {
		for _, older := range w.order {
			if !w.at[older].Before(w.at[ref]) {
				continue
			}
			j, launched := launchedAt[older]
			if launched && j > i {
				t.Fatalf("job %v (submitted at %v) launched before %v (submitted at %v)", ref, w.at[ref], older, w.at[older])
			}
		}
	}
}

// A job cancelled while queued is never launched, and every job ends in exactly one final state.
func TestACancelledQueuedJobNeverRuns(t *testing.T) {
	for seed := range uint64(200) {
		w := newWorld(t, seed, 2)
		for range 6 {
			w.submit()
		}
		victim := w.order[len(w.order)-1]
		w.feed(lifecycle.CancelAsked{Ref: victim, Reason: "no longer needed", At: w.tick()})
		w.drain()
		if w.store[victim] != core.JobCancelled {
			t.Fatalf("seed %d: the cancelled job is %q, want cancelled", seed, w.store[victim])
		}
		if slices.Contains(w.launches, victim) {
			t.Fatalf("seed %d: the job was cancelled while queued and still launched", seed)
		}
	}
}

// crash is a daemon restart: the machine and its unanswered requests are gone, and the store, the
// supervisors and the run directories stay. Recovery feeds the machine what it finds.
func (w *world) crash() {
	w.state = lifecycle.Recovering()
	w.pending = nil
}

func (w *world) recoverAll(upTo int) {
	w.t.Helper()
	events := 0
	for _, ref := range w.order {
		presence := lifecycle.PresenceNone
		switch s := w.sup[ref]; {
		case s != nil && s.alive:
			presence = lifecycle.PresenceAlive
		case w.dirs[ref]:
			presence = lifecycle.PresenceGone
		}
		if events == upTo {
			return
		}
		events++
		w.feed(lifecycle.Recovered{Ref: ref, State: w.store[ref], SubmittedAt: w.at[ref], Supervisor: presence, At: w.tick()})
	}
}

// Recovery is replay: a restart at any point, including in the middle of a restart, ends with
// every job final, a job that was running without a supervisor lost and not retried, and a job
// that was running with one adopted.
func TestRecoveryAfterACrashAtAnyPointSettlesEveryJob(t *testing.T) {
	for seed := range uint64(300) {
		w := newWorld(t, seed, 1+int(seed%3))
		w.churn(10+w.r.IntN(60), false)

		// Supervisors may have died while the daemon was down.
		for _, ref := range w.order {
			if s := w.sup[ref]; s != nil && s.alive && w.r.IntN(4) == 0 {
				s.alive = false
			}
		}
		ranBefore := map[lifecycle.Ref]bool{}
		for ref, st := range w.store {
			if st == core.JobRunning {
				ranBefore[ref] = true
			}
		}
		launchedBefore := len(w.launches)

		w.crash()
		w.recoverAll(w.r.IntN(len(w.order) + 1)) // a restart that is cut short...
		w.crash()                                // ...and a second restart that is not
		w.recoverAll(len(w.order))
		w.feed(lifecycle.RecoveryDone{})
		w.drain()

		for _, ref := range w.launches[launchedBefore:] {
			if ranBefore[ref] {
				t.Fatalf("seed %d: job %v was running before the restart and was launched again", seed, ref)
			}
		}
		for ref := range ranBefore {
			if st := w.store[ref]; !st.Final() {
				t.Fatalf("seed %d: job %v was running before the restart and is %q after", seed, ref, st)
			}
		}
	}
}

func TestAStartWithNothingToRecoverIsAnEmptyMachine(t *testing.T) {
	var s lifecycle.State
	if held := s.Held(); len(held) != 0 {
		t.Fatalf("a new machine holds %+v", held)
	}
	if q, r := s.Counts(); q != 0 || r != 0 {
		t.Fatalf("a new machine counts %d queued and %d running", q, r)
	}
}

func TestStepLeavesTheStateItWasGivenAsItWas(t *testing.T) {
	first, ref := lifecycle.Ref{Store: "s", Job: "j1"}, lifecycle.Ref{Store: "s", Job: "j2"}
	before, _ := lifecycle.Step(lifecycle.State{}, lifecycle.Admitted{Ref: first, SubmittedAt: epoch})
	before, _ = lifecycle.Step(before, lifecycle.Admitted{Ref: ref, SubmittedAt: epoch})
	held := before.Held()
	after, reqs := lifecycle.Step(before, lifecycle.CancelAsked{Ref: ref, Reason: "x", At: epoch})
	if !slices.Equal(held, before.Held()) {
		t.Fatalf("Step changed its input: %+v became %+v", held, before.Held())
	}
	if len(after.Held()) != 2 || len(reqs) != 1 {
		t.Fatalf("cancelling a queued job left %+v and asked for %+v", after.Held(), reqs)
	}
}

// Jobs submitted at one instant, in different stores, start in the order the machine heard of
// them, and each starts only after the one before has been recorded as ended.
func TestJobsSubmittedAtOneInstantStartInArrivalOrder(t *testing.T) {
	var s lifecycle.State
	refs := []lifecycle.Ref{{Store: "a", Job: "j1"}, {Store: "b", Job: "j1"}, {Store: "a", Job: "j2"}}
	var launched []lifecycle.Ref
	for _, ref := range refs {
		var reqs []lifecycle.Request
		s, reqs = lifecycle.Step(s, lifecycle.Admitted{Ref: ref, SubmittedAt: epoch})
		for _, r := range reqs {
			launched = append(launched, r.(lifecycle.LaunchRequest).Ref)
		}
	}
	if len(launched) != 1 {
		t.Fatalf("three jobs arrived and %d were launched, want 1", len(launched))
	}
	for len(launched) < len(refs) {
		prev := launched[len(launched)-1]
		s, _ = lifecycle.Step(s, lifecycle.Launched{Ref: prev})
		var reqs []lifecycle.Request
		s, reqs = lifecycle.Step(s, lifecycle.Finished{Ref: prev, Outcome: lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecExited}}, At: epoch})
		if slices.ContainsFunc(reqs, func(r lifecycle.Request) bool { _, ok := r.(lifecycle.LaunchRequest); return ok }) {
			t.Fatalf("the next job was launched before %v was recorded as ended", prev)
		}
		s, reqs = lifecycle.Step(s, lifecycle.Closed{Ref: prev})
		for _, r := range reqs {
			launched = append(launched, r.(lifecycle.LaunchRequest).Ref)
		}
	}
	if !slices.Equal(launched, refs) {
		t.Fatalf("launch order %v, want %v", launched, refs)
	}
}
