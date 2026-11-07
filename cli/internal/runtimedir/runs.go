package runtimedir

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sync"
)

// Runs is which runs of the daemon's placements live, kept as locks on files of a directory: a run
// lives while a process holds the lock of its scope, and the system drops the lock when that
// process ends, however it ends. A placement whose jobs run at once, each in a scope of its own, uses
// it to tell what a dead job left, which a run clears, from what a live one is using. Create it with
// NewRuns; it is safe for concurrent use.
//
// The lock file of a dead run is removed by whoever proves it dead (Claim), while it holds the lock.
// So a lock is taken only on the file that is at its path once it is held: one taken on a file
// removed meanwhile is let go and taken again.
type Runs struct {
	dir string

	mu   sync.Mutex
	held map[string]*os.File // the runs this process holds, by scope
}

// NewRuns returns the runs kept in dir, which is made when a run is first held.
func NewRuns(dir string) *Runs { return &Runs{dir: dir, held: map[string]*os.File{}} }

// PlacementRuns is the directory of the locks of the runs of the daemon's placements (Runs).
func (d Dir) PlacementRuns() string { return filepath.Join(d.path, "placement-runs") }

var runScope = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$`)

func (r *Runs) path(scope string) (string, error) {
	if !runScope.MatchString(scope) {
		return "", fmt.Errorf("%q is not the scope of a run", scope)
	}
	return filepath.Join(r.dir, scope+".lock"), nil
}

// Hold marks the run of the scope live for as long as this process lives. A run this process holds
// already is held. It waits while another process clears what a dead run of the scope left, or
// while a process of the run lives, up to ctx.
func (r *Runs) Hold(ctx context.Context, scope string) error {
	p, err := r.path(scope)
	if err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.held[scope]; ok {
		return nil
	}
	if err := os.MkdirAll(r.dir, 0o700); err != nil {
		return fmt.Errorf("making the directory of the runs' locks: %w", err)
	}
	for {
		l, err := WaitLock(ctx, p)
		if err != nil {
			return err
		}
		if current(l.f, p) {
			r.held[scope] = l.f
			return nil
		}
		l.Unlock()
	}
}

// Claim reports whether the run of the scope is dead: no process holds it. A dead run is held for
// the caller until it calls release, which removes its lock file, so that no run of the scope
// starts while the caller clears what it left. release is nil for a live run, and one this process
// holds is live.
func (r *Runs) Claim(scope string) (release func(), dead bool, err error) {
	p, err := r.path(scope)
	if err != nil {
		return nil, false, err
	}
	r.mu.Lock()
	_, mine := r.held[scope]
	r.mu.Unlock()
	if mine {
		return nil, false, nil
	}
	if err := os.MkdirAll(r.dir, 0o700); err != nil {
		return nil, false, fmt.Errorf("making the directory of the runs' locks: %w", err)
	}
	for {
		l, err := TryLock(p)
		if errors.Is(err, ErrHeld) {
			return nil, false, nil
		}
		if err != nil {
			return nil, false, err
		}
		if !current(l.f, p) {
			l.Unlock()
			continue
		}
		return func() {
			_ = os.Remove(p)
			l.Unlock()
		}, true, nil
	}
}

// current reports whether the open file is the one at the path.
func current(f *os.File, path string) bool {
	held, err := f.Stat()
	if err != nil {
		return false
	}
	now, err := os.Stat(path)
	if err != nil {
		return false
	}
	return os.SameFile(held, now)
}
