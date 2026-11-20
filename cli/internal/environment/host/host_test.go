//go:build unix

package host_test

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/host"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/gpu"
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

// A job given GPUs of the machine's pool is told them in the variables of the vendor's role and in
// the words every vendor shares, in every command it starts; one given none is told nothing of them,
// not even what the daemon's own environment holds; and a grant of another pool is refused before
// anything starts.
func TestACommandOnTheHostIsToldTheGPUsItWasGiven(t *testing.T) {
	t.Setenv("VIBESYS_GPUS", "left-over")
	t.Setenv("VIBESYS_GPU_DEVICES", "left-over")
	size, err := core.ParseGPUCount(8, "test")
	if err != nil {
		t.Fatal(err)
	}
	devices, err := gpu.NewDevices(gpu.DevicesConfig{Machine: "local", Size: size})
	if err != nil {
		t.Fatal(err)
	}
	group, err := core.ParseDeviceGroup([]uint32{2, 3, 6}, size, "test")
	if err != nil {
		t.Fatal(err)
	}
	id, _ := devices.Pool()
	read := func(grant *core.Grant) (core.Attempt, string) {
		var out bytes.Buffer
		ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
		defer cancel()
		env := host.New(host.Config{Backend: machine(), Timers: clock.System{}, Devices: devices, GPUs: grant})
		a := env.Run(ctx, core.Execution{
			Checkout: checkout(t, nil), Timeout: time.Hour,
			Argv: []string{"/bin/sh", "-c", `printf '%s|%s|%s|%s' "$CUDA_VISIBLE_DEVICES" "$CUDA_DEVICE_ORDER" "$VIBESYS_GPUS" "$VIBESYS_GPU_DEVICES"`},
		}, &out)
		return a, out.String()
	}
	a, got := read(&core.Grant{Pool: id, Size: size, Devices: group, Source: core.GrantByCLI})
	if e, ok := a.(core.Ended); !ok || e.Exec.ExitCode != 0 || got != "2,3,6|PCI_BUS_ID|3|2,3,6" {
		t.Errorf("a job given GPUs 2,3,6: attempt %+v, output %q", a, got)
	}
	if a, got := read(nil); got != "|||" {
		t.Errorf("a job given none: attempt %+v, output %q, want none of the variables", a, got)
	}
	other := &core.Grant{Pool: "gpu:host-b", Size: size, Devices: group, Source: core.GrantByCLI}
	if a, got := read(other); got != "" {
		t.Errorf("a grant of another pool: attempt %+v, output %q, want it refused before anything starts", a, got)
	} else if ns, ok := a.(core.NotStarted); !ok || ns.Failure.Kind != core.FailureInvalidInput {
		t.Errorf("a grant of another pool: attempt %+v, want not started, invalid input", a)
	}
}

// The role's check of a job's devices runs on the host before any of its commands: one that fails,
// as one whose devices another process holds does, ends the job not started, with what the check
// said, and its command never runs; one that passes lets it run. A job given no devices runs no check.
func TestAJobWhoseDevicesAreHeldDoesNotStartOnTheHost(t *testing.T) {
	size, err := core.ParseGPUCount(4, "test")
	if err != nil {
		t.Fatal(err)
	}
	group, err := core.ParseDeviceGroup([]uint32{0, 1}, size, "test")
	if err != nil {
		t.Fatal(err)
	}
	said := "neuron: /dev/neuron0 holds device memory on 2 of its 8 physical cores"
	run := func(check string, grant bool) (core.Attempt, bool) {
		devices := &fakeresource.Devices{Of: core.ResourceNeuron, ID: "neuron:local", Size: size, CheckArgv: []string{"/bin/sh", "-c", check}}
		var g *core.Grant
		if grant {
			g = &core.Grant{Pool: "neuron:local", Size: size, Devices: group, Source: core.GrantByCLI}
		}
		dir := checkout(t, nil)
		ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
		defer cancel()
		env := host.New(host.Config{Backend: machine(), Timers: clock.System{}, Devices: devices, GPUs: g})
		a := env.Run(ctx, core.Execution{Checkout: dir, Timeout: time.Hour, Argv: []string{"/bin/sh", "-c", "touch ran"}}, &bytes.Buffer{})
		_, statErr := os.Stat(filepath.Join(dir.Dir, "ran"))
		return a, statErr == nil
	}
	a, ran := run("echo '"+said+"'; exit 1", true)
	if ns, ok := a.(core.NotStarted); !ok || ns.Failure.Kind != core.FailureUnavailable || !strings.Contains(ns.Failure.Message, said) || ran {
		t.Errorf("a failing check: attempt %+v, the command ran %v; want not started with what the check said", a, ran)
	}
	if a, ran := run("exit 0", true); !ran {
		t.Errorf("a passing check: attempt %+v, the command did not run", a)
	}
	if a, ran := run("exit 1", false); !ran {
		t.Errorf("a job given no devices: attempt %+v, the command did not run", a)
	}
}
