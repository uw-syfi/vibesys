package docker_test

import (
	"context"
	"fmt"
	"io"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

var machineFacts = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}

func cpuBackend() backend.Backend {
	return cpu.New(cpu.NewFakeSystem(machineFacts), clock.NewManual(time.Unix(1, 0), time.Millisecond))
}

const simpleDockerfile = "FROM busybox:1.36.1\nRUN true\n"

// rig is a docker environment over a fake daemon, with everything a test wants to look at.
type rig struct {
	daemon   *dockerfake.Daemon
	client   *dockerfake.Client
	provider *docker.Provider
	ids      *dockerfake.IDs
}

type options struct {
	spec    docker.Spec
	process uint32
	backend backend.Backend
	timers  clock.Timers
}

func newRig(t testing.TB, daemon *dockerfake.Daemon, o options) *rig {
	t.Helper()
	if o.spec.Dockerfile == nil {
		o.spec.Dockerfile = []byte(simpleDockerfile)
	}
	if o.backend == nil {
		o.backend = cpuBackend()
	}
	if o.timers == nil {
		o.timers = envtest.NewTimers()
	}
	client := daemon.Connect()
	ids := dockerfake.NewIDs(o.process)
	p, err := docker.New(docker.Config{
		Engine: client, Source: dockerfake.Fixed(o.spec), Backend: o.backend, Timers: o.timers,
		IDs: ids, Scope: "test", User: docker.User{UID: 1000, GID: 1000},
	})
	if err != nil {
		t.Fatal(err)
	}
	return &rig{daemon: daemon, client: client, provider: p, ids: ids}
}

func (r *rig) env(t testing.TB) environment.Environment {
	t.Helper()
	e, err := r.provider.Environment(environment.Target{Repository: "/repo", Commit: "0123456789abcdef0123456789abcdef01234567", Task: "task"})
	if err != nil {
		t.Fatal(err)
	}
	return e
}

var checkoutCount atomic.Uint64

// checkout makes a directory name that is valid for a mount and unique to the case.
func checkout(t *testing.T) core.Checkout {
	return core.Checkout{
		Commit: core.Commit("0123456789abcdef0123456789abcdef01234567"),
		Dir:    fmt.Sprintf("/fake-checkouts/c%d", checkoutCount.Add(1)),
	}
}

// The docker environment over the fake daemon is held to the same contract as the host.
func TestDockerOverTheFakeEnginePassesTheEnvironmentContract(t *testing.T) {
	envtest.Run(t, envtest.Rig{
		New: func(t *testing.T, w envtest.World, timers clock.Timers) environment.Environment {
			daemon := dockerfake.NewDaemon(w.(fakeworld.World).MemoryGates)
			t.Cleanup(func() {
				if running := daemon.Running(); len(running) > 0 {
					t.Errorf("containers still run after the case: %v", running)
				}
			})
			return newRig(t, daemon, options{timers: timers}).env(t)
		},
		World:    func(t *testing.T) envtest.World { return fakeworld.New() },
		Checkout: checkout,
		// The fake has no process to kill. The nearest thing is the death of the connection,
		// which is what the death of the process is to the engine: what the connection held
		// must stop by itself.
		Spawn: func(t *testing.T, w envtest.World, x core.Execution) func() {
			daemon := dockerfake.NewDaemon(w.(fakeworld.World).MemoryGates)
			r := newRig(t, daemon, options{timers: envtest.NewTimers()})
			var wg sync.WaitGroup
			wg.Go(func() { _, _ = r.env(t).Run(context.Background(), x, io.Discard) })
			var once sync.Once
			kill := func() { once.Do(func() { r.client.Crash(); wg.Wait() }) }
			t.Cleanup(kill)
			return kill
		},
	})
}
