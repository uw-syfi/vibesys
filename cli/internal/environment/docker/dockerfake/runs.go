package dockerfake

import (
	"context"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// RunTable is which runs live, kept in memory and shared by the processes of a test, as the locks
// of runtimedir.Runs are by the processes of a machine. Each process has its own view of it
// (Process), whose holds end when it dies (Die).
type RunTable struct {
	mu      sync.Mutex
	holders map[string]*Runs // the process that holds each scope
	changed chan struct{}    // closed and made again whenever a hold ends
}

// NewRunTable returns a table where no run lives.
func NewRunTable() *RunTable {
	return &RunTable{holders: map[string]*Runs{}, changed: make(chan struct{})}
}

// Process returns the view of a new process.
func (t *RunTable) Process() *Runs { return &Runs{t: t} }

// Live returns whether a process holds the run of the scope.
func (t *RunTable) Live(scope string) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	_, ok := t.holders[scope]
	return ok
}

func (t *RunTable) end(scope string) {
	delete(t.holders, scope)
	close(t.changed)
	t.changed = make(chan struct{})
}

// Runs is one process's docker.Runs over a RunTable.
type Runs struct {
	t    *RunTable
	dead bool
}

var _ docker.Runs = (*Runs)(nil)

// Hold implements docker.Runs.
func (r *Runs) Hold(ctx context.Context, scope string) error {
	for {
		r.t.mu.Lock()
		holder, held := r.t.holders[scope]
		if !held && !r.dead {
			r.t.holders[scope] = r
		}
		changed := r.t.changed
		r.t.mu.Unlock()
		if !held || holder == r {
			return nil
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Claim implements docker.Runs. The claim is a hold of the caller's until release.
func (r *Runs) Claim(scope string) (func(), bool, error) {
	r.t.mu.Lock()
	defer r.t.mu.Unlock()
	if _, held := r.t.holders[scope]; held {
		return nil, false, nil
	}
	claim := &Runs{t: r.t}
	r.t.holders[scope] = claim
	return func() {
		r.t.mu.Lock()
		defer r.t.mu.Unlock()
		if r.t.holders[scope] == claim {
			r.t.end(scope)
		}
	}, true, nil
}

// Die ends the process: every run it holds is dead.
func (r *Runs) Die() {
	r.t.mu.Lock()
	defer r.t.mu.Unlock()
	r.dead = true
	for scope, holder := range r.t.holders {
		if holder == r {
			r.t.end(scope)
		}
	}
}
