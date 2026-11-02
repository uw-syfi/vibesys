package fakeenv_test

import (
	"context"
	"io"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

func newFake(w envtest.World, timers clock.Timers) environment.Environment {
	return fakeenv.New(core.EnvironmentHost, w.(fakeworld.World).MemoryGates, fakeenv.WithTimers(timers))
}

// The fake is held to the same contract as the host environment, so a test that runs a job on the
// fake means what it would mean on a machine.
func TestFakePassesTheEnvironmentContract(t *testing.T) {
	envtest.Run(t, envtest.Rig{
		New: func(t *testing.T, w envtest.World, timers clock.Timers) environment.Environment {
			return newFake(w, timers)
		},
		World: func(t *testing.T) envtest.World { return fakeworld.New() },
		Checkout: func(t *testing.T, _ envtest.World) core.Checkout {
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

type files map[string][]byte

func (f files) Put(path string, data []byte) { f[path] = data }

func TestEmitWritesTheOutputFileTheCommandWasGiven(t *testing.T) {
	put := files{}
	env := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates(), fakeenv.WithFiles(put))
	x := core.Execution{
		Checkout: core.Checkout{Commit: "0123456789abcdef0123456789abcdef01234567", Dir: "/fake-checkouts/c"},
		Argv:     []string{"emit", "a", "b", ";", "exit", "3", "--out", "/scratch/x.out"},
		Timeout:  1,
	}
	got, err := env.Run(context.Background(), x, io.Discard)
	if err != nil || got.Kind != core.ExecExited || got.ExitCode != 3 {
		t.Fatalf("Run = %+v, %v", got, err)
	}
	if string(put["/scratch/x.out"]) != "a b" || len(put) != 1 {
		t.Errorf("files = %q", put)
	}
}

func TestEmitFailsLikeAnyCommandThatCannotWriteItsFile(t *testing.T) {
	x := core.Execution{
		Checkout: core.Checkout{Commit: "0123456789abcdef0123456789abcdef01234567", Dir: "/fake-checkouts/c"},
		Timeout:  1,
	}
	for name, c := range map[string]struct {
		files fakeenv.Files
		argv  []string
	}{
		"no files":        {nil, []string{"emit", "x", "--out", "/scratch/x.out"}},
		"no output given": {files{}, []string{"emit", "x"}},
		"relative path":   {files{}, []string{"emit", "x", "--out", "x.out"}},
	} {
		t.Run(name, func(t *testing.T) {
			var opts []fakeenv.Option
			if c.files != nil {
				opts = append(opts, fakeenv.WithFiles(c.files))
			}
			env := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates(), opts...)
			x.Argv = c.argv
			if got, err := env.Run(context.Background(), x, io.Discard); err != nil || got.ExitCode != 127 {
				t.Errorf("Run = %+v, %v; want exit 127", got, err)
			}
		})
	}
}

// A Machine is the place a fake container runs its commands: a script runs and reports how it
// ended, and ending the machine ends every process it started, the ones in the background too.
func TestMachineRunsScriptsAndEndsEveryProcess(t *testing.T) {
	gates := fakeenv.NewMemoryGates()
	m := fakeenv.NewMachine(gates, "/work")
	var out syncBuffer
	p, err := m.Start([]string{"dir", ";", "print", "hi", ";", "exit", "3"}, &out)
	if err != nil {
		t.Fatal(err)
	}
	if exit := <-p.Exited(); exit.Code != 3 || exit.Err != nil {
		t.Fatalf("exit %+v, want code 3", exit)
	}
	if got, want := out.String(), "/work\nhi\n"; got != want {
		t.Errorf("output %q, want %q", got, want)
	}

	if _, err := m.Start([]string{"alive", "leader", ";", "child", "{", "alive", "background", ";", "hold", "forever", "}", ";", "hold", "forever"}, &out); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if !gates.Reached(ctx, fakeenv.LiveMark("background")) {
		t.Fatal("the background process never started")
	}
	m.End()
	m.End() // ending twice is the same as once
	for _, name := range []string{"leader", "background"} {
		if !gates.Gone(ctx, name) {
			t.Errorf("the process %q still runs after the machine ended", name)
		}
	}
}

type syncBuffer struct {
	mu  sync.Mutex
	buf strings.Builder
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}
