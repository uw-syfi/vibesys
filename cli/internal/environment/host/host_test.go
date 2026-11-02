//go:build unix

package host_test

import (
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
