package alloc_test

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

var epoch = time.Date(2026, 11, 10, 8, 0, 0, 0, time.UTC)

// The placements every test of this package uses: a cluster's, with time limits and a margin, one
// with a queue timeout, a machine's slots with no end, and one that offers no standing allocations.
const (
	gpu   = "gpu"
	queue = "queue"
	local = "local"
	off   = "off"
)

var config = alloc.Config{Placements: map[string]alloc.Limits{
	gpu:   {MaxHeld: 2, MaxFor: 24 * time.Hour, Margin: 5 * time.Minute},
	queue: {MaxHeld: 1, MaxFor: 12 * time.Hour, QueueTimeout: 6 * time.Hour},
	local: {MaxHeld: 1, MaxFor: 24 * time.Hour, Endless: true},
}}

var everywhere = []string{gpu, queue, local, off}

// world drives Step the way the daemon would, one event at a time, keeping the clock and every
// action, and checks the invariants after each step.
type world struct {
	t       *testing.T
	cfg     alloc.Config
	now     time.Time
	s       alloc.State
	log     []alloc.Input
	actions []alloc.Action
}

func newWorld(t *testing.T) *world {
	t.Helper()
	return &world{t: t, cfg: config, now: epoch}
}

func (w *world) pass(d time.Duration) { w.now = w.now.Add(d) }

// do steps one event, checks every invariant across the step, and returns its actions.
func (w *world) do(e alloc.Event) []alloc.Action {
	w.t.Helper()
	before := w.s
	after, actions := alloc.Step(w.s, w.cfg, w.now, e)
	if err := checkStep(w.cfg, w.now, before, e, after, actions); err != nil {
		w.t.Fatalf("after %T%+v at %s: %v", e, e, w.now.Sub(epoch), err)
	}
	w.s = after
	w.log = append(w.log, alloc.Input{At: w.now, Event: e})
	w.actions = append(w.actions, actions...)
	return actions
}

// accept steps a request that must be accepted.
func (w *world) accept(e alloc.Event) []alloc.Action {
	w.t.Helper()
	actions := w.do(e)
	if r, ok := refusal(actions); ok {
		w.t.Fatalf("%T%+v refused: %s", e, e, r.Message())
	}
	return actions
}

// refuse steps a request that must be refused with a reason of the type of want, and returns it.
func (w *world) refuse(e alloc.Event, want alloc.Refusal) alloc.Refusal {
	w.t.Helper()
	actions := w.do(e)
	r, ok := refusal(actions)
	if !ok {
		w.t.Fatalf("%T%+v accepted (%v), want it refused with %T", e, e, actions, want)
	}
	if reflect.TypeOf(r) != reflect.TypeOf(want) {
		w.t.Fatalf("%T%+v refused with %T (%s), want %T", e, e, r, r.Message(), want)
	}
	return r
}

func refusal(actions []alloc.Action) (alloc.Refusal, bool) {
	for _, a := range actions {
		if r, ok := a.(alloc.Refuse); ok {
			return r.Reason, true
		}
	}
	return nil, false
}

func (w *world) get(id alloc.ID) alloc.Allocation {
	w.t.Helper()
	a, ok := w.s.Get(id)
	if !ok {
		w.t.Fatalf("no allocation %s", id)
	}
	return a
}

func (w *world) stage(id alloc.ID) alloc.Stage { w.t.Helper(); return w.get(id).Stage() }

func (w *world) ended(id alloc.ID) alloc.Ended {
	w.t.Helper()
	p, ok := w.get(id).Phase.(alloc.Ended)
	if !ok {
		w.t.Fatalf("%s is %s, want ended", id, w.stage(id))
	}
	return p
}

func (w *world) live(id alloc.ID) alloc.Live {
	w.t.Helper()
	switch p := w.get(id).Phase.(type) {
	case alloc.Running:
		return p.Live
	case alloc.Draining:
		return p.Live
	case alloc.Pending, alloc.Ended:
	}
	w.t.Fatalf("%s is %s, want running or draining", id, w.stage(id))
	return alloc.Live{}
}

// ask asks for an allocation for run-a and returns its ID.
func (w *world) ask(placement string, d time.Duration) alloc.ID {
	w.t.Helper()
	return w.askFor("run-a", placement, d)
}

func (w *world) askFor(run, placement string, d time.Duration) alloc.ID {
	w.t.Helper()
	for _, a := range w.accept(alloc.Ask{Run: coreRun(run), Placement: placement, For: d}) {
		if s, ok := a.(alloc.Submit); ok {
			return s.Spec.ID
		}
	}
	w.t.Fatalf("an accepted ask made no Submit")
	return ""
}

// took has the backend take the allocation's submission.
func (w *world) took(id alloc.ID) {
	w.t.Helper()
	w.do(alloc.Submitted{ID: id, Handle: alloc.Handle{Alloc: id, Ref: "ref-" + string(id)}})
}

// start has the backend start the allocation now, with no end of its own to say.
func (w *world) start(id alloc.ID) {
	w.t.Helper()
	w.do(alloc.Observed{ID: id, Seen: alloc.SeenRunning{Node: "node-a", StartedAt: w.now}})
}

// running is a Running allocation for run-a on the placement, taken and started.
func (w *world) running(placement string, d time.Duration) alloc.ID {
	w.t.Helper()
	id := w.ask(placement, d)
	w.took(id)
	w.start(id)
	if w.stage(id) != alloc.StageRunning {
		w.t.Fatalf("%s is %s after it started", id, w.stage(id))
	}
	return id
}

// submit submits a job of run-a into the allocation (empty for any) and returns the actions.
func (w *world) submit(job string, bound time.Duration, into alloc.ID) []alloc.Action {
	w.t.Helper()
	return w.do(alloc.JobSubmitted{Run: "run-a", Job: coreJob(job), Bound: bound, Into: into, Eligible: everywhere})
}

// has says whether the actions hold one equal to want.
func has(actions []alloc.Action, want alloc.Action) bool {
	for _, a := range actions {
		if reflect.DeepEqual(a, want) {
			return true
		}
	}
	return false
}

func kinds(actions []alloc.Action) []string {
	out := make([]string, len(actions))
	for i, a := range actions {
		out[i] = fmt.Sprintf("%T", a)
	}
	return out
}

func contains(s, sub string) bool { return strings.Contains(s, sub) }
