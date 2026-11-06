//go:build unix

package host_test

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/host"
)

var facts = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}

func machine() backend.Backend {
	return cpu.New(cpu.NewFakeSystem(facts), clock.NewManual(time.Unix(1, 0), time.Millisecond))
}

func newHost(timers clock.Timers) environment.Environment {
	return host.New(host.Config{Backend: machine(), Timers: timers})
}

// TestMain lets the test binary be the process the suite kills: see envtest.HelperMain.
func TestMain(m *testing.M) {
	if envtest.HelperMain(func() environment.Environment { return newHost(clock.System{}) }) {
		return
	}
	os.Exit(m.Run())
}

func checkout(t *testing.T, _ envtest.World) core.Checkout {
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return core.Checkout{Commit: core.Commit("0123456789abcdef0123456789abcdef01234567"), Dir: dir}
}

// The host environment is held to the contract by running real shell commands.
func TestHostPassesTheEnvironmentContract(t *testing.T) {
	envtest.Run(t, envtest.Rig{
		New: func(t *testing.T, _ envtest.World, timers clock.Timers) environment.Environment {
			return newHost(timers)
		},
		World:    func(t *testing.T) envtest.World { return envtest.NewShellWorld(t) },
		Checkout: checkout,
		Spawn: func(t *testing.T, _ envtest.World, x core.Execution) func() {
			return envtest.SpawnProcess(t, x)
		},
	})
}

// A command on the host is told about its job as one in a container is: where its artifacts go and
// how long it may run, and nothing the daemon's own environment says about another job.
func TestACommandOnTheHostIsToldItsJobEnvironment(t *testing.T) {
	t.Setenv("VIBESYS_MOUNTS", "left-over")
	co := checkout(t, nil)
	artifacts, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	a := newHost(clock.System{}).Run(ctx, core.Execution{
		Checkout: co, Artifacts: artifacts, Timeout: time.Hour,
		Argv: []string{"/bin/sh", "-c", `printf '%s\n%s\n%s' "$VIBESYS_ARTIFACTS_DIR" "$VIBESYS_JOB_TIMEOUT_SECONDS" "$VIBESYS_MOUNTS"`},
	}, &out)
	if e, ok := a.(core.Ended); !ok || e.Exec.ExitCode != 0 {
		t.Fatalf("attempt %+v", a)
	}
	if want := artifacts + "\n3600\n"; out.String() != want {
		t.Errorf("the command printed %q, want %q", out.String(), want)
	}
}

// The grace period the operator configures is the one a stop waits for, and the default is
// environment.DefaultGrace.
func TestTheConfiguredGraceIsWhatAStopWaitsFor(t *testing.T) {
	for _, grace := range []time.Duration{0, 7 * time.Second} {
		want := grace
		if want == 0 {
			want = environment.DefaultGrace
		}
		w := envtest.NewShellWorld(t)
		timers := envtest.NewTimers()
		env := host.New(host.Config{Backend: machine(), Timers: timers, Grace: grace})
		ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
		runCtx, stop := context.WithCancel(ctx)
		done := make(chan core.Attempt, 1)
		go func() {
			done <- env.Run(runCtx, core.Execution{
				Checkout: checkout(t, w),
				Argv:     w.Argv(envtest.Program{envtest.IgnoreTerm(), envtest.Mark("up"), envtest.Hold(envtest.Forever)}),
				Timeout:  time.Hour,
			}, nil)
		}()
		if !w.Reached(ctx, "up") {
			t.Fatal("the command never started")
		}
		stop()
		timer := timers.Await(ctx, 2)
		if timer == nil || timer.Duration != want {
			t.Fatalf("grace %v: the stop armed %+v, want a timer of %v", grace, timer, want)
		}
		timer.Fire()
		select {
		case a := <-done:
			envtest.Ended(t, a)
		case <-ctx.Done():
			t.Fatal("the run did not end after the grace period")
		}
		cancel()
	}
}
