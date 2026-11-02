// Package daemontest is a test support package: daemons that run inside the test process, on a
// real runtime directory and a real socket, over the fakes of this repository. A test that needs
// a daemon without the cost of a process uses it; the end-to-end tests run the real binary.
package daemontest

import (
	"context"
	"errors"
	"io"
	"os"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// Starter is a client.Starter whose daemons run in this process. Each Start runs daemon.Run on
// the runtime directory with the runtime that Runtime returns, and returns once it serves.
// Runtime is called for every start, so a restarted daemon is made of what the test keeps: its
// stores and its supervisors.
type Starter struct {
	Dir     runtimedir.Dir
	Version string
	Runtime func() daemon.Runtime
	Clock   clock.Clock

	calls atomic.Int64

	mu      sync.Mutex
	running []*running
}

type running struct {
	stop context.CancelFunc
	done chan error
}

// Start implements client.Starter.
func (s *Starter) Start(ctx context.Context) error {
	s.calls.Add(1)
	r, w, err := os.Pipe()
	if err != nil {
		return err
	}
	defer r.Close()
	// The daemon outlives the call that started it, so it is bound to the starter and not to ctx.
	runCtx, stop := context.WithCancel(context.Background())
	p := &running{stop: stop, done: make(chan error, 1)}
	s.mu.Lock()
	s.running = append(s.running, p)
	s.mu.Unlock()
	clk := s.Clock
	if clk == nil {
		clk = clock.System{}
	}
	go func() {
		p.done <- daemon.Run(runCtx, daemon.Config{
			Dir: s.Dir, Version: s.Version, Runtime: s.Runtime(), Clock: clk, Ready: w,
		})
	}()
	buf := make([]byte, 1)
	if _, err := io.ReadFull(r, buf); err != nil {
		// A daemon that failed closed the pipe without a byte.
		return <-p.done
	}
	if buf[0] == runtimedir.ReadyTaken {
		return <-p.done
	}
	return nil
}

// Calls returns how many times Start was called.
func (s *Starter) Calls() int { return int(s.calls.Load()) }

// Close stops every daemon this starter started and waits for them to return. It fails the test
// if one returned an error.
func (s *Starter) Close(t testing.TB) {
	t.Helper()
	s.mu.Lock()
	all := s.running
	s.running = nil
	s.mu.Unlock()
	for _, p := range all {
		p.stop()
	}
	for _, p := range all {
		if err := <-p.done; err != nil && !errors.Is(err, daemon.ErrAlreadyRunning) {
			t.Errorf("a daemon returned %v", err)
		}
	}
}
