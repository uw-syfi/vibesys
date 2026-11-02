package app

import (
	"context"
	"errors"
	"slices"
	"sync/atomic"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/changed"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// App is the daemon's job flows. Create it with New, call Start once, and Close it to stop.
//
// One goroutine, the loop, owns everything that changes: the machine's state, and which store and
// which supervisor handle a job has. Everything else reports to it. Carrying out a request of the
// machine is an effect, a goroutine that only calls an interface and posts what came of it, and a
// call from a client reads the store and posts events the same way. So the order of events is the
// only order that matters, and every effect is owned and joined by Close.
type App struct {
	cfg       Config
	ctx       context.Context
	cancel    context.CancelFunc
	group     owned.Group
	startedAt time.Time

	inbox chan message
	view  atomic.Pointer[view]

	jobsChanged changed.Signal
	logsChanged changed.Signal
}

// message is an event for the machine, with what the loop must keep because of it.
type message struct {
	event lifecycle.Event
	// scope, when set, is the scope of the store the event's job is in.
	scope *Scope
	// handle, when set, reaches the event's job's supervisor.
	handle supervisor.Handle
	// then, when set, is a goroutine to start after the event. It goes through the loop so that
	// nothing is started while Close waits.
	then func()
	// done, when set, is closed once the loop has taken the event up.
	done chan struct{}
}

// view is what the loop publishes for calls to read.
type view struct {
	queued, running int
	// trouble is the jobs whose end is out of the daemon's hands for now, and why.
	trouble map[lifecycle.Ref]core.Failure
	// waits is the queued jobs held back by another job's or their store's trouble, and why.
	waits map[lifecycle.Ref]core.Failure
	// held is the jobs the machine holds.
	held map[lifecycle.Ref]bool
	// unread is the stores whose jobs the machine has not read.
	unread []Scope
}

// New returns an App that is recovering: it launches nothing until Start has told it what the
// stores and the supervisors hold.
func New(cfg Config) *App {
	cfg = cfg.withDefaults()
	cfg.Stores = boundedStores{inner: cfg.Stores, timers: cfg.Timers, limit: cfg.CallTimeout}
	ctx, cancel := context.WithCancel(context.Background())
	a := &App{
		cfg: cfg, ctx: ctx, cancel: cancel, startedAt: cfg.Clock.Now(),
		inbox: make(chan message),
	}
	a.view.Store(&view{})
	a.group.Go(a.loop)
	return a
}

// Close stops the App: what it was waiting for is abandoned, and it returns once everything it
// started has returned. Supervisors are not touched: they outlive the daemon, and the next start
// finds them. Close may be called more than once.
func (a *App) Close() {
	a.cancel()
	a.group.Wait()
}

// post gives the loop a message. It returns false when the App is closing.
func (a *App) post(m message) bool {
	select {
	case a.inbox <- m:
		return true
	case <-a.ctx.Done():
		if m.handle != nil {
			m.handle.Close()
		}
		return false
	}
}

// settle posts a message and returns when the loop has taken it up, or the App closes.
func (a *App) settle(m message) {
	m.done = make(chan struct{})
	if !a.post(m) {
		return
	}
	select {
	case <-m.done:
	case <-a.ctx.Done():
	}
}

// tell posts an event that needs nothing else kept.
func (a *App) tell(e lifecycle.Event) bool { return a.post(message{event: e}) }

// loop is the only place the machine steps.
func (a *App) loop() {
	state := lifecycle.Recovering()
	scopes := map[lifecycle.StoreID]Scope{}
	handles := map[lifecycle.Ref]supervisor.Handle{}
	defer func() {
		for _, h := range handles {
			h.Close()
		}
	}()
	for {
		var m message
		select {
		case m = <-a.inbox:
		case <-a.ctx.Done():
			return
		}
		if m.scope != nil {
			scopes[storeIDOf(*m.scope)] = *m.scope
		}
		var reqs []lifecycle.Request
		state, reqs = lifecycle.Step(state, m.event)
		// Before any effect starts: whoever sees an effect's work in a store finds the counts
		// already past it.
		a.publish(state, scopes)
		keepHandle(handles, m, reqs, state)
		for _, r := range reqs {
			a.startEffect(r, scopes, handles)
		}
		switch e := m.event.(type) {
		case lifecycle.Closed:
			dropHandle(handles, e.Ref)
		case lifecycle.Discarded:
			dropHandle(handles, e.Ref)
		case lifecycle.StoreForgotten:
			delete(scopes, e.Store)
			for ref := range handles {
				if ref.Store == e.Store {
					dropHandle(handles, ref)
				}
			}
		default:
			// The rest leave the job in the machine, with whatever it holds.
		}
		if m.then != nil {
			a.group.Go(m.then)
		}
		if m.done != nil {
			close(m.done)
		}
	}
}

// keepHandle keeps a message's handle if the machine asked for something of its job, or keeps the
// job for its turn. One that a probe found before the machine moved on is not the job's, which may
// have a newer run by now.
func keepHandle(handles map[lifecycle.Ref]supervisor.Handle, m message, reqs []lifecycle.Request, state lifecycle.State) {
	if m.handle == nil {
		return
	}
	ref, hasRef := refOf(m.event)
	asked := slices.ContainsFunc(reqs, func(r lifecycle.Request) bool { return r.Target() == ref })
	if !hasRef || (!asked && !state.Parked(ref)) {
		m.handle.Close()
		return
	}
	dropHandle(handles, ref)
	handles[ref] = m.handle
}

func dropHandle(handles map[lifecycle.Ref]supervisor.Handle, ref lifecycle.Ref) {
	if h, ok := handles[ref]; ok {
		h.Close()
		delete(handles, ref)
	}
}

// refOf is the job an event is about.
func refOf(e lifecycle.Event) (lifecycle.Ref, bool) {
	switch e := e.(type) {
	case lifecycle.Admitted:
		return e.Ref, true
	case lifecycle.Recovered:
		return e.Ref, true
	case lifecycle.CancelAsked:
		return e.Ref, true
	case lifecycle.Launched:
		return e.Ref, true
	case lifecycle.LaunchFailed:
		return e.Ref, true
	default:
		// The rest bring no handle.
		return lifecycle.Ref{}, false
	}
}

// publish makes the machine's state readable, and wakes whoever new trouble concerns.
func (a *App) publish(s lifecycle.State, scopes map[lifecycle.StoreID]Scope) {
	next := &view{trouble: map[lifecycle.Ref]core.Failure{}, waits: map[lifecycle.Ref]core.Failure{}, held: map[lifecycle.Ref]bool{}}
	next.queued, next.running = s.Counts()
	for _, t := range s.Troubled() {
		next.trouble[t.Ref] = t.Failure
	}
	for _, t := range s.Waiting() {
		next.waits[t.Ref] = t.Failure
	}
	for _, h := range s.Held() {
		next.held[h.Ref] = true
	}
	for _, id := range s.Unread() {
		if scope, ok := scopes[id]; ok {
			next.unread = append(next.unread, scope)
		}
	}
	last := a.view.Load()
	a.view.Store(next)
	// Only what is new ends a wait. One that passes is seen at the next change of the job.
	if appeared(next.trouble, last.trouble) || appeared(next.waits, last.waits) {
		a.notifyJobs()
	}
}

// appeared says whether a job has a failure it did not have before.
func appeared(next, last map[lifecycle.Ref]core.Failure) bool {
	for ref := range next {
		if _, was := last[ref]; !was {
			return true
		}
	}
	return false
}

// troubleOf says what keeps a job from ending, if anything does.
func (a *App) troubleOf(ref lifecycle.Ref) (core.Failure, bool) {
	f, ok := a.view.Load().trouble[ref]
	return f, ok
}

// waitOf says what a job waits for, when something other than its turn holds it: its own trouble,
// or that of what stands before it.
func (a *App) waitOf(ref lifecycle.Ref) (core.Failure, bool) {
	if f, ok := a.troubleOf(ref); ok {
		return f, true
	}
	f, ok := a.view.Load().waits[ref]
	return f, ok
}

func (a *App) now() time.Time { return a.cfg.Clock.Now() }

// notifyJobs wakes whoever waits on a change of a job's state or log.
func (a *App) notifyJobs() {
	a.jobsChanged.Notify()
	a.logsChanged.Notify()
}

// failureOf is the failure an error stands for.
func failureOf(err error) core.Failure {
	var f *core.Failure
	if errors.As(err, &f) {
		return *f
	}
	return core.Failure{Kind: core.FailureInternal, Message: err.Error()}
}

// Status is what the daemon reports about its work.
type Status struct {
	StartedAt time.Time
	Queued    int
	Running   int
}

// Status returns the machine's counts.
func (a *App) Status() Status {
	v := a.view.Load()
	return Status{StartedAt: a.startedAt, Queued: v.queued, Running: v.running}
}
