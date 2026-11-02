//go:build unix

package flowrun_test

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/host"
	"github.com/uw-syfi/vibesys/cli/internal/flowrun"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
)

// dirCheckouts makes a real directory per checkout. It is the real part of what a git checkout is
// to the flow: a directory the commands run in and that is removed afterwards.
type dirCheckouts struct {
	base string

	mu   sync.Mutex
	open int
}

func (c *dirCheckouts) Checkout(ctx context.Context, _ string, commit core.Commit) (core.Checkout, func(), error) {
	if err := ctx.Err(); err != nil {
		return core.Checkout{}, nil, err
	}
	dir, err := os.MkdirTemp(c.base, "checkout-")
	if err != nil {
		return core.Checkout{}, nil, err
	}
	c.mu.Lock()
	c.open++
	c.mu.Unlock()
	var once sync.Once
	return core.Checkout{Commit: commit, Dir: dir}, func() {
		once.Do(func() {
			_ = os.RemoveAll(dir)
			c.mu.Lock()
			c.open--
			c.mu.Unlock()
		})
	}, nil
}

// realRig is the shell on the host environment, the operating system's file system and real
// directories. Its machine is a fake system, which the backend's own tests hold to the real one.
type realRig struct {
	scratch   string
	checkouts *dirCheckouts
	clock     *clock.Manual
	backend   *cpu.Backend
	env       *host.Environment
}

func newRealRig(t *testing.T) rig {
	t.Helper()
	base, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	be := cpu.New(cpu.NewFakeSystem(facts), clk)
	return &realRig{
		scratch: filepath.Join(base, "scratch"), checkouts: &dirCheckouts{base: base}, clock: clk, backend: be,
		env: host.New(host.Config{Backend: be, Timers: clock.System{}}),
	}
}

func (*realRig) real() bool { return true }

func (r *realRig) config(out io.Writer) flowrun.Config {
	_ = os.MkdirAll(r.scratch, 0o700)
	return flowrun.Config{
		Repository: "/repo", Commit: commit, Environment: r.env, Checkouts: r.checkouts,
		Backend: r.backend, Files: hostfs.OS{Base: r.scratch}, Clock: r.clock, Output: out,
	}
}

func (r *realRig) leaks() []string {
	var left []string
	entries, _ := os.ReadDir(r.scratch)
	for _, e := range entries {
		left = append(left, e.Name())
	}
	r.checkouts.mu.Lock()
	defer r.checkouts.mu.Unlock()
	if r.checkouts.open != 0 {
		left = append(left, fmt.Sprintf("%d checkouts", r.checkouts.open))
	}
	return left
}

// The scenarios the fakes pass are passed by real processes, real files and real directories, so
// a job on the fakes means what it would mean on a machine.
func TestScenariosOnTheRealHost(t *testing.T)     { runScenarios(t, newRealRig) }
func TestCancellationsOnTheRealHost(t *testing.T) { runCancellations(t, newRealRig) }
func TestOutputOnTheRealHost(t *testing.T)        { runOutput(t, newRealRig) }
