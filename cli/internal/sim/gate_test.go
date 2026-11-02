package sim_test

import (
	"context"
	"errors"
	"slices"
	"strings"
	"sync"
	"testing/synctest"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// verdict is how the driver lets a call go on.
type verdict int

const (
	pass verdict = iota
	// failBefore fails the call with no effect.
	failBefore
	// failAfter takes the effect and fails the call, as a lost reply does.
	failAfter
	// crashBefore and crashAfter are the same with the daemon dying at this call.
	crashBefore
	crashAfter
	// hang is a call that never answers, which only its caller's deadline ends.
	hang
)

var (
	errInjected = &core.Failure{Kind: core.FailureInternal, Message: "injected failure"}
	errCrashed  = errors.New("the daemon crashed")
)

// call is one Fake call waiting for the driver.
type call struct {
	label  string
	write  bool
	daemon *incarnation
	decide chan verdict
	// effected is closed once a crashAfter call has taken its effect.
	effected chan struct{}
}

// gatekeeper holds the calls that wait for the driver.
type gatekeeper struct {
	mu      sync.Mutex
	pending []*call
}

func (g *gatekeeper) remove(c *call) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.pending = slices.DeleteFunc(g.pending, func(p *call) bool { return p == c })
}

// enter waits for the driver's verdict on a call of a daemon. A call of a dead daemon has no effect
// and no driver.
func (g *gatekeeper) enter(ctx context.Context, d *incarnation, label string, write bool) (verdict, *call, error) {
	if d.dead.Load() {
		return crashBefore, nil, nil
	}
	c := &call{
		label: actorOf(ctx) + " " + label, write: write, daemon: d,
		decide: make(chan verdict, 1), effected: make(chan struct{}),
	}
	g.mu.Lock()
	g.pending = append(g.pending, c)
	g.mu.Unlock()
	select {
	case v := <-c.decide:
		return v, c, nil
	case <-ctx.Done():
		g.remove(c)
		return pass, c, ctx.Err()
	}
}

// sorted returns the calls that wait, in an order that does not depend on when they arrived.
func (g *gatekeeper) sorted() []*call {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := slices.Clone(g.pending)
	slices.SortStableFunc(out, func(a, b *call) int { return strings.Compare(a.label, b.label) })
	return out
}

// dropDaemon answers every call of a dead daemon.
func (g *gatekeeper) dropDaemon(d *incarnation) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.pending = slices.DeleteFunc(g.pending, func(p *call) bool {
		if p.daemon != d {
			return false
		}
		p.decide <- crashBefore
		return true
	})
}

// gated runs a Fake call under the driver's verdict.
func gated[T any](ctx context.Context, w *world, d *incarnation, label string, write bool, do func() (T, error)) (T, error) {
	var zero T
	v, c, err := w.gate.enter(ctx, d, label, write)
	if err != nil {
		return zero, err
	}
	switch v {
	case failBefore:
		return zero, errInjected
	case hang:
		select {
		case <-ctx.Done():
			return zero, ctx.Err()
		case <-d.died:
			return zero, errCrashed
		}
	case failAfter:
		_, _ = do()
		return zero, errInjected
	case crashBefore:
		<-d.died
		return zero, errCrashed
	case crashAfter:
		_, _ = do()
		close(c.effected)
		<-d.died
		return zero, errCrashed
	}
	return do()
}

// gatedStore is one store as a daemon reaches it. Every call waits for the driver. A method that is
// not listed here is one the daemon does not call, and panics through the nil embedded interface
// of a test that starts to.
type gatedStore struct {
	store.Store
	scope app.Scope
	w     *world
	d     *incarnation
}

func (s gatedStore) Submit(ctx context.Context, sub core.Submission) (core.SubmitOutcome, error) {
	return gated(ctx, s.w, s.d, "store.Submit", true, func() (core.SubmitOutcome, error) { return s.Store.Submit(ctx, sub) })
}

func (s gatedStore) Job(ctx context.Context, id core.JobID) (core.Job, error) {
	return gated(ctx, s.w, s.d, "store.Job "+string(id), false, func() (core.Job, error) {
		j, err := s.Store.Job(ctx, id)
		if err == nil && strings.HasPrefix(actorOf(ctx), "cancel") {
			s.w.noteCancelRead(s.d, s.scope, j)
		}
		return j, err
	})
}

func (s gatedStore) Jobs(ctx context.Context, f core.JobFilter) ([]core.Job, error) {
	return gated(ctx, s.w, s.d, "store.Jobs", false, func() ([]core.Job, error) { return s.Store.Jobs(ctx, f) })
}

func (s gatedStore) Transition(ctx context.Context, t core.Transition) (core.Job, error) {
	label := "store.Transition " + string(t.Job) + " " + string(t.From) + ">" + string(t.To)
	return gated(ctx, s.w, s.d, label, true, func() (core.Job, error) {
		return s.Store.Transition(ctx, t)
	})
}

func (s gatedStore) AddNote(ctx context.Context, id core.JobID, n core.Note) (core.Job, error) {
	return gated(ctx, s.w, s.d, "store.AddNote "+string(id), true, func() (core.Job, error) { return s.Store.AddNote(ctx, id, n) })
}

func (s gatedStore) AppendLog(ctx context.Context, id core.JobID, data []byte) error {
	_, err := gated(ctx, s.w, s.d, "store.AppendLog "+string(id), true, func() (struct{}, error) {
		s.w.noteAppend(s.scope, id)
		return struct{}{}, s.Store.AppendLog(ctx, id, data)
	})
	return err
}

func (s gatedStore) ReadLog(ctx context.Context, id core.JobID, off uint64, max int) (core.LogChunk, error) {
	return gated(ctx, s.w, s.d, "store.ReadLog "+string(id), false, func() (core.LogChunk, error) { return s.Store.ReadLog(ctx, id, off, max) })
}

// gatedHandle is a supervisor as a daemon reaches it.
type gatedHandle struct {
	inner supervisor.Handle
	ref   lifecycle.Ref
	w     *world
	d     *incarnation
}

func (h gatedHandle) call(ctx context.Context, what string, do func() error) error {
	_, err := gated(ctx, h.w, h.d, what+" "+h.ref.String(), true, func() (struct{}, error) { return struct{}{}, do() })
	return err
}

func (h gatedHandle) Start(ctx context.Context) error {
	return h.call(ctx, "sup.Start", func() error {
		h.w.noteStart(h.d, h.ref)
		return h.inner.Start(ctx)
	})
}

func (h gatedHandle) Cancel(ctx context.Context, reason string) error {
	return h.call(ctx, "sup.Cancel", func() error {
		h.w.noteCancelSent(h.ref, reason)
		return h.inner.Cancel(ctx, reason)
	})
}

func (h gatedHandle) Release(ctx context.Context) error {
	return h.call(ctx, "sup.Release", func() error {
		h.w.noteRelease(h.ref)
		return h.inner.Release(ctx)
	})
}

func (h gatedHandle) Follow(ctx context.Context, off uint64, emit func([]byte) error) (lifecycle.Outcome, error) {
	label := "sup.Follow " + h.ref.String()
	_, err := gated(ctx, h.w, h.d, label, false, func() (struct{}, error) { return struct{}{}, nil })
	if err != nil {
		return lifecycle.Outcome{}, err
	}
	// The driver can break the stream while it is open.
	fctx, sever := context.WithCancelCause(ctx)
	defer sever(nil)
	h.w.addFollow(h.ref, sever)
	defer h.w.dropFollow(h.ref)
	out, err := h.inner.Follow(fctx, off, emit)
	if cause := context.Cause(fctx); cause != nil && errors.Is(cause, errInjected) {
		return lifecycle.Outcome{}, errInjected
	}
	return out, err
}

// gatedSupervisors is the Supervisors of a daemon.
type gatedSupervisors struct {
	inner supervisor.Supervisors
	w     *world
	d     *incarnation
}

func (s gatedSupervisors) wrap(h supervisor.Handle, ref lifecycle.Ref) supervisor.Handle {
	return gatedHandle{inner: h, ref: ref, w: s.w, d: s.d}
}

func (s gatedSupervisors) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	h, err := gated(ctx, s.w, s.d, "sup.Spawn "+l.Ref().String(), false, func() (supervisor.Handle, error) {
		s.w.noteSpawn(l.Ref())
		return s.inner.Spawn(ctx, l)
	})
	if err != nil {
		return nil, err
	}
	return s.wrap(h, l.Ref()), nil
}

func (s gatedSupervisors) Runs(ctx context.Context) ([]supervisor.Run, error) {
	runs, err := gated(ctx, s.w, s.d, "sup.Runs", false, func() ([]supervisor.Run, error) { return s.inner.Runs(ctx) })
	if err != nil {
		return nil, err
	}
	slices.SortFunc(runs, func(a, b supervisor.Run) int { return strings.Compare(a.Ref.String(), b.Ref.String()) })
	for i := range runs {
		runs[i] = s.w.probed(runs[i])
		if runs[i].Handle != nil {
			runs[i].Handle = s.wrap(runs[i].Handle, runs[i].Ref)
		}
	}
	return runs, nil
}

func (s gatedSupervisors) Probe(ctx context.Context, ref lifecycle.Ref) (supervisor.Run, error) {
	run, err := gated(ctx, s.w, s.d, "sup.Probe "+ref.String(), false, func() (supervisor.Run, error) { return s.inner.Probe(ctx, ref) })
	if err != nil {
		return supervisor.Run{}, err
	}
	run = s.w.probed(run)
	if run.Handle != nil {
		run.Handle = s.wrap(run.Handle, ref)
	}
	return run, nil
}

func (s gatedSupervisors) Forget(ctx context.Context, ref lifecycle.Ref) error {
	_, err := gated(ctx, s.w, s.d, "sup.Forget "+ref.String(), true, func() (struct{}, error) {
		s.w.noteRelease(ref)
		return struct{}{}, s.inner.Forget(ctx, ref)
	})
	return err
}

// simGates are the gates a command waits at. The driver opens a gate when it picks the command.
type simGates struct {
	mu      sync.Mutex
	open    map[string]bool
	waiting map[string]chan struct{}
}

var _ fakeenv.Gates = (*simGates)(nil)

func newSimGates() *simGates {
	return &simGates{open: map[string]bool{}, waiting: map[string]chan struct{}{}}
}

// Wait implements fakeenv.Gates.
func (g *simGates) Wait(ctx context.Context, name string) bool {
	g.mu.Lock()
	if g.open[name] {
		g.mu.Unlock()
		return true
	}
	ch := make(chan struct{})
	g.waiting[name] = ch
	g.mu.Unlock()
	select {
	case <-ch:
		return true
	case <-ctx.Done():
		g.mu.Lock()
		delete(g.waiting, name)
		g.mu.Unlock()
		return false
	}
}

// names returns the gates a command waits at, in order.
func (g *simGates) names() []string {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make([]string, 0, len(g.waiting))
	for name := range g.waiting {
		out = append(out, name)
	}
	slices.Sort(out)
	return out
}

func (g *simGates) release(name string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.open[name] = true
	if ch, ok := g.waiting[name]; ok {
		close(ch)
		delete(g.waiting, name)
	}
}

// simTimers are timers the driver fires by moving the time on. Timers due at the same moment fire
// together, so the order they were armed in does not matter.
type simTimers struct {
	clock *clock.Manual

	mu    sync.Mutex
	now   time.Duration
	armed int
	due   map[*simTimer]bool
}

type simTimer struct {
	at    time.Duration
	armed int
	fired chan struct{}
}

var _ clock.Timers = (*simTimers)(nil)

func newSimTimers(c *clock.Manual) *simTimers {
	return &simTimers{clock: c, due: map[*simTimer]bool{}}
}

// Timeout implements clock.Timers.
func (s *simTimers) Timeout(d time.Duration) (<-chan struct{}, func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.armed++
	t := &simTimer{at: s.now + d, armed: s.armed, fired: make(chan struct{})}
	s.due[t] = true
	return t.fired, func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		delete(s.due, t)
	}
}

// next says when the next timer is due, and false when none is armed.
func (s *simTimers) next() (time.Duration, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var at time.Duration
	found := false
	for t := range s.due {
		if !found || t.at < at {
			at, found = t.at, true
		}
	}
	return at, found
}

// advance moves the time to at and fires every timer due by then, in the order they fall due and
// armed. Each one settles before the next fires: timers firing together would race into the
// daemon's inbox, and the scheduler would pick the order of its events and not the seed.
func (s *simTimers) advance(at time.Duration) {
	s.mu.Lock()
	if at > s.now {
		s.clock.Advance(at - s.now)
		s.now = at
	}
	s.mu.Unlock()
	for s.fireOne() {
		synctest.Wait()
	}
}

// fireOne fires the first timer that is due, and reports false when none is.
func (s *simTimers) fireOne() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	var first *simTimer
	for t := range s.due {
		if t.at > s.now {
			continue
		}
		if first == nil || t.at < first.at || (t.at == first.at && t.armed < first.armed) {
			first = t
		}
	}
	if first == nil {
		return false
	}
	close(first.fired)
	delete(s.due, first)
	return true
}
