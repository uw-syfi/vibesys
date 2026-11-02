package fakeenv_test

import (
	"context"
	"io"
	"path/filepath"
	"strconv"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

// world is the fake's side of the contract suite's World: the in-memory gates and points, and a
// renderer from a Program to the fake's script.
type world struct{ *fakeenv.MemoryGates }

func (world) Missing() []string { return []string{"no-such-command"} }

func (world) Argv(p envtest.Program) []string {
	if len(p) == 0 {
		return []string{"exit", "0"} // a command line cannot be empty
	}
	return render(p)
}

func render(p envtest.Program) []string {
	var out []string
	for i, s := range p {
		if i > 0 {
			out = append(out, ";")
		}
		switch s.Op {
		case envtest.OpPrint:
			out = append(out, "print", s.Text)
		case envtest.OpWarn:
			out = append(out, "warn", s.Text)
		case envtest.OpDir:
			out = append(out, "dir")
		case envtest.OpMark:
			out = append(out, "mark", s.Name)
		case envtest.OpHold:
			out = append(out, "hold", s.Name)
		case envtest.OpExit:
			out = append(out, "exit", strconv.Itoa(s.Code))
		case envtest.OpCrash:
			out = append(out, "crash")
		case envtest.OpChild:
			out = append(out, "child", "{")
			out = append(out, render(s.Body)...)
			out = append(out, "}")
		case envtest.OpAlive:
			out = append(out, "alive", s.Name)
		case envtest.OpUnlessFlag:
			out = append(out, "unless-flag", s.Name)
		default:
			panic("fakeenv_test: no rendering for " + string(s.Op))
		}
	}
	return out
}

func newFake(w envtest.World, timers clock.Timers) environment.Environment {
	return fakeenv.New(core.EnvironmentHost, w.(world).MemoryGates, fakeenv.WithTimers(timers))
}

// The fake is held to the same contract as the host environment, so a test that runs a job on the
// fake means what it would mean on a machine.
func TestFakePassesTheEnvironmentContract(t *testing.T) {
	envtest.Run(t, envtest.Rig{
		New: func(t *testing.T, w envtest.World, timers clock.Timers) environment.Environment {
			return newFake(w, timers)
		},
		World: func(t *testing.T) envtest.World { return world{fakeenv.NewMemoryGates()} },
		Checkout: func(t *testing.T) core.Checkout {
			return core.Checkout{Commit: "0123456789abcdef0123456789abcdef01234567", Dir: filepath.Join("/", "fake-checkouts", t.Name())}
		},
		// The fake has no process to kill. The nearest thing is the run being abandoned: its
		// context ends, and what the run started must end with it.
		Spawn: func(t *testing.T, w envtest.World, x core.Execution) func() {
			ctx, cancel := context.WithCancel(context.Background())
			env := newFake(w, envtest.NewTimers())
			var wg sync.WaitGroup
			wg.Go(func() { _, _ = env.Run(ctx, x, io.Discard) })
			var once sync.Once
			kill := func() { once.Do(func() { cancel(); wg.Wait() }) }
			t.Cleanup(kill)
			return kill
		},
	})
}
