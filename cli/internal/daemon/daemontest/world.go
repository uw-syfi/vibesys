package daemontest

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// World is everything that outlives a daemon: its stores (the disk), its supervisors (their
// processes), and the machine's gates. A daemon started on the same World after another stopped
// is a daemon that restarts.
type World struct {
	// Gates are the gates a fake command waits at: `hold g` waits for Gates.Release("g").
	Gates     *fakeenv.MemoryGates
	Env       *fakeenv.Environment
	Checkouts *fakeenv.Checkouts
	Stores    *memstores.Stores
	Survivors *supervisor.Survivors
	// Branches are the repositories' result branches.
	Branches *gitrepo.FakeBranches
	// Git holds the repositories callers' directories resolve in, for the daemon and for a command
	// line that runs on this World: AddRepo makes one, AddWorktree a linked worktree of it.
	Git *gitrepo.Fake
	// Tasks holds the repositories the daemons read tasks from: Put the files of a commit to give
	// it a task. A benchmark's `emit` writes into Files, where the job reads its result from.
	Tasks *task.MemorySource
	Files *hostfs.Memory
	// Docker is the World's docker environment, on the same gates and files as Env: a task with a
	// Dockerfile runs its jobs, and holds its labs, there.
	Docker *fakeenv.Environment
	// Artifacts, when set before a daemon starts, is where its jobs keep their artifacts, which a
	// lab needs. Nil keeps none.
	Artifacts func(repository, task string, job core.JobID) string
}

// Machine is what the World's environment reads of the machine its commands run on.
var Machine = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}

// NewWorld returns a World that is cleaned up with the test, and fails the test if a command is
// still running or a checkout open when it ends.
func NewWorld(t testing.TB) *World {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	files := hostfs.NewMemory()
	machine := cpu.New(cpu.NewFakeSystem(Machine), clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond))
	w := &World{
		Gates: gates, Env: fakeenv.New(core.EnvironmentHost, gates, fakeenv.WithFiles(files), fakeenv.WithMachine(machine)), Checkouts: &fakeenv.Checkouts{},
		Stores: memstores.New(app.Rules(task.DefaultSamples)), Survivors: supervisor.NewSurvivors(),
		Branches: gitrepo.NewFakeBranches(), Git: gitrepo.NewFake(), Tasks: task.NewMemorySource(), Files: files,
		Docker: fakeenv.New(core.EnvironmentDocker, gates, fakeenv.WithFiles(files), fakeenv.WithMachine(machine)),
	}
	t.Cleanup(func() {
		w.Survivors.Close()
		if n := w.Env.Active() + w.Docker.Active(); n != 0 {
			t.Errorf("%d commands still run after the test", n)
		}
		if n := w.Checkouts.Open(); n != 0 {
			t.Errorf("%d checkouts still open after the test", n)
		}
	})
	return w
}

// Runtime returns what a daemon on this World runs on.
func (w *World) Runtime() daemon.Runtime {
	envs := fakeenv.Environments{core.EnvironmentHost: w.Env, core.EnvironmentDocker: w.Docker}
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	host := supervisor.Host{Files: w.Files, Clock: clk, Artifacts: w.Artifacts}
	return daemon.Runtime{
		Stores: w.Stores, Environments: envs, Branches: w.Branches, Repositories: w.Git, Tasks: w.Tasks, Files: w.Tasks,
		Supervisors: supervisor.NewInProcess(envs, w.Checkouts, w.Survivors).WithHost(host),
	}
}

type localFS struct{}

func (localFS) IsNetwork(string) (bool, error) { return false, nil }

// RuntimeDir opens a runtime directory for the test, on a path short enough for a socket, and
// removes it when the test ends.
func RuntimeDir(t testing.TB) runtimedir.Dir {
	t.Helper()
	root, err := os.MkdirTemp("", "vs")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	dir, err := runtimedir.Open(filepath.Join(root, "rt"), os.Getuid(), localFS{})
	if err != nil {
		t.Fatal(err)
	}
	return dir
}
