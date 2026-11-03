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

// A command on the host finds its artifacts directory in the variable a container's command does.
func TestACommandOnTheHostIsToldItsArtifactsDirectory(t *testing.T) {
	co := checkout(t, nil)
	artifacts, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	a := newHost(clock.System{}).Run(ctx, core.Execution{
		Checkout: co, Artifacts: artifacts, Argv: []string{"/bin/sh", "-c", `printf %s "$VIBESYS_ARTIFACTS_DIR"`}, Timeout: time.Hour,
	}, &out)
	if e, ok := a.(core.Ended); !ok || e.Exec.ExitCode != 0 || out.String() != artifacts {
		t.Errorf("attempt %+v printed %q, want %q", a, out.String(), artifacts)
	}
}
