package app

import (
	"context"
	"maps"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// ownedSupervisors makes the App the owner of every connection to a supervisor that a call
// returns. A call that opens one, Spawn, Runs or Probe, can answer at any moment, including the
// moment Close cancels the App, and the code that receives the connection hands it on to the loop,
// to a release or to a deferred Close. Each hand-off is a place where one exit (a return that
// skips it because the App is closing, an error path that never posted it) can drop the
// connection with nobody left to close it. So the connection is recorded here, where it is made,
// and closeAll gives up what is left once no call of the App can still use it.
//
// Whoever finishes with a connection earlier still closes it, so a daemon that runs for days
// holds only the connections in use. A connection closes once, whoever asks first. A call that
// answers after closeAll gets a connection that is closed already, so the daemon's own calls and
// the client calls that race with Close leave nothing open either.
type ownedSupervisors struct {
	supervisor.Supervisors

	mu     sync.Mutex
	open   map[*ownedHandle]struct{}
	closed bool
}

func newOwnedSupervisors(inner supervisor.Supervisors) *ownedSupervisors {
	return &ownedSupervisors{Supervisors: inner, open: map[*ownedHandle]struct{}{}}
}

var _ supervisor.Supervisors = (*ownedSupervisors)(nil)

// Spawn implements supervisor.Supervisors. It owns whatever handle comes back, with an error or
// without.
func (o *ownedSupervisors) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	h, err := o.Supervisors.Spawn(ctx, l)
	return o.own(h), err
}

// Runs implements supervisor.Supervisors.
func (o *ownedSupervisors) Runs(ctx context.Context) ([]supervisor.Run, error) {
	runs, err := o.Supervisors.Runs(ctx)
	if runs == nil {
		return nil, err
	}
	mine := make([]supervisor.Run, len(runs))
	for i, run := range runs {
		mine[i] = o.ownRun(run)
	}
	return mine, err
}

// Probe implements supervisor.Supervisors.
func (o *ownedSupervisors) Probe(ctx context.Context, ref lifecycle.Ref) (supervisor.Run, error) {
	run, err := o.Supervisors.Probe(ctx, ref)
	if run == nil {
		return nil, err
	}
	return o.ownRun(run), err
}

// ownRun owns the handle of a run that has one. The other runs hold none.
func (o *ownedSupervisors) ownRun(run supervisor.Run) supervisor.Run {
	if alive, ok := run.(supervisor.Alive); ok {
		alive.Handle = o.own(alive.Handle)
		return alive
	}
	return run
}

// own records a handle and returns the one the App uses in its place.
func (o *ownedSupervisors) own(h supervisor.Handle) supervisor.Handle {
	if h == nil {
		return nil
	}
	mine := &ownedHandle{Handle: h, owner: o}
	o.mu.Lock()
	closed := o.closed
	if !closed {
		o.open[mine] = struct{}{}
	}
	o.mu.Unlock()
	if closed {
		mine.Close()
	}
	return mine
}

// closeAll gives up every connection still open, and every one that is opened later. It may be
// called more than once.
func (o *ownedSupervisors) closeAll() {
	o.mu.Lock()
	left := slices.Collect(maps.Keys(o.open))
	o.open, o.closed = nil, true
	o.mu.Unlock()
	for _, h := range left {
		h.Close()
	}
}

// ownedHandle is a connection its owner has recorded. It closes the one underneath once.
type ownedHandle struct {
	supervisor.Handle
	owner *ownedSupervisors
	once  sync.Once
}

var _ supervisor.Handle = (*ownedHandle)(nil)

// Close implements supervisor.Handle.
func (h *ownedHandle) Close() {
	h.once.Do(func() {
		h.owner.mu.Lock()
		delete(h.owner.open, h)
		h.owner.mu.Unlock()
		h.Handle.Close()
	})
}
