package daemontest

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// World is everything that outlives a daemon: its stores (the disk), its supervisors (their
// processes), and the machine's gates. A daemon started on the same World after another stopped
// is a daemon that restarts.
type World struct {
	// Gates are the gates a fake command waits at: `hold g` waits for Gates.Release("g").
	Gates     *fakeenv.MemoryGates
	Env       *fakeenv.Environment
	Checkouts *fakeenv.Checkouts
	Stores    *app.MemoryStores
	Survivors *supervisor.Survivors
}

// NewWorld returns a World that is cleaned up with the test, and fails the test if a command is
// still running or a checkout open when it ends.
func NewWorld(t testing.TB) *World {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	w := &World{
		Gates: gates, Env: fakeenv.New(core.EnvironmentHost, gates), Checkouts: &fakeenv.Checkouts{},
		Stores: app.NewMemoryStores(app.Rules(5)), Survivors: supervisor.NewSurvivors(),
	}
	t.Cleanup(func() {
		w.Survivors.Close()
		if n := w.Env.Active(); n != 0 {
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
	envs := fakeenv.Environments{core.EnvironmentHost: w.Env}
	return daemon.Runtime{
		Stores: w.Stores, Environments: envs,
		Supervisors: supervisor.NewInProcess(envs, w.Checkouts, w.Survivors),
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
