package assemble_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release/assemble"
)

// machineWorld is a machine with one user and a vibesys-cli on it, as that tool behaves from the
// environment each of its processes is run with (cli/README.md, "The daemon and the supervisors", which the
// real tool is held to by cli/cmd/vibesys-cli's real tier). The user may have a daemon running, which
// holds the machine lock whatever its runtime directory; a process with no XDG_RUNTIME_DIR of its own
// reaches the default runtime directory; a sandbox daemon holds no lock, and needs a runtime directory of
// its own that is not the holder's.
type machineWorld struct {
	uid int
	// holder is the runtime directory of the user's daemon, empty when none runs.
	holder string

	// What the world saw.
	reachedHolder []string // commands that ran against the user's daemon
	holderStopped bool
	started       []string // runtime directories of daemons a command started
	holdsMachine  []string // of those, the ones that hold the machine lock
	programs      map[string]bool
}

func (w *machineWorld) runtimeOf(env map[string]string) string {
	if dir := env["XDG_RUNTIME_DIR"]; filepath.IsAbs(dir) {
		return filepath.Join(dir, "vibesys-cli")
	}
	return fmt.Sprintf("/tmp/vibesys-cli-%d", w.uid)
}

// daemonFor is the runtime directory of the daemon a command of this environment talks to, starting
// one when none runs there, or the error the tool gives.
func (w *machineWorld) daemonFor(env map[string]string) (string, error) {
	runtime := w.runtimeOf(env)
	switch env["VIBESYS_CLI_SANDBOX"] {
	case "1":
		if !filepath.IsAbs(env["XDG_RUNTIME_DIR"]) {
			return "", errors.New("VIBESYS_CLI_SANDBOX=1 is a sandbox daemon, which needs a runtime directory of its own: set XDG_RUNTIME_DIR to an absolute directory")
		}
		if runtime == w.holder {
			return "", errors.New("VIBESYS_CLI_SANDBOX=1 is a sandbox daemon, and its runtime directory is that of the daemon that holds this machine")
		}
	case "":
		switch {
		case runtime == w.holder:
			w.reachedHolder = append(w.reachedHolder, runtime)
			return runtime, nil
		case w.holder != "":
			return "", fmt.Errorf("another vibesys-cli daemon of this user holds this machine: runtime directory %s", w.holder)
		}
		w.holdsMachine = append(w.holdsMachine, runtime)
	default:
		return "", errors.New("VIBESYS_CLI_SANDBOX is not 1")
	}
	if !slices.Contains(w.started, runtime) {
		w.started = append(w.started, runtime)
	}
	return runtime, nil
}

func (w *machineWorld) Run(_ context.Context, program string, args, environ []string) ([]byte, []byte, error) {
	if w.programs == nil {
		w.programs = map[string]bool{}
	}
	w.programs[program] = true
	env := map[string]string{}
	for _, kv := range environ {
		name, value, _ := strings.Cut(kv, "=")
		if _, dup := env[name]; dup {
			return nil, nil, fmt.Errorf("%s is set twice", name)
		}
		env[name] = value
	}
	switch {
	case len(args) == 6 && slices.Equal(args[:4], []string{"task", "card", "--json", "--folder"}):
		if _, err := w.daemonFor(env); err != nil {
			return nil, []byte(err.Error()), errors.New("exit status 1")
		}
		out, err := protojson.Marshal(&cliv1.TaskCard{Task: args[5], Heading: "heading of " + args[5]})
		return out, nil, err
	case slices.Equal(args, []string{"daemon", "stop"}):
		runtime := w.runtimeOf(env)
		if runtime == w.holder {
			w.holderStopped = true
		}
		w.started = slices.DeleteFunc(w.started, func(r string) bool { return r == runtime })
		return nil, nil, nil
	}
	return nil, nil, fmt.Errorf("unexpected command %v", args)
}

// The build's card read shares nothing with the daemon of the person who builds, whichever that
// daemon is: not its machine lock (the failure that stopped a release bundle build while a user's
// daemon ran), not its socket, and not its life, since the build ends by stopping the daemon it
// started. Over every way a user's daemon may be placed, the reads succeed, none reaches the user's
// daemon, the user's daemon is not stopped, and every daemon the build started is in its own state
// folder, holds no machine lock, and is gone when the build is.
func TestTheBuildsCardReadSharesNothingWithTheUsersDaemon(t *testing.T) {
	r := rand.New(rand.NewPCG(21, 22))
	const uid = 501
	holders := []string{
		"",                                      // no daemon runs
		fmt.Sprintf("/tmp/vibesys-cli-%d", uid), // the default runtime directory, which a bare environment reaches
		"/run/user/501/vibesys-cli",
		"/home/someone/.cache/run/vibesys-cli",
	}
	for range 200 {
		holder := holders[r.IntN(len(holders))]
		if holder == "" && r.IntN(2) == 0 {
			holder = fmt.Sprintf("/tmp/other-%d/vibesys-cli", r.IntN(1000))
		}
		state := fmt.Sprintf("/tmp/vsb%d", r.IntN(1e9))
		// The operator's own environment, which a build run from a sandboxed shell has settings in.
		t.Setenv("XDG_RUNTIME_DIR", strings.TrimSuffix(holder, "/vibesys-cli"))
		t.Setenv("VIBESYS_CLI_SANDBOX", []string{"", "1"}[r.IntN(2)])
		world := &machineWorld{uid: uid, holder: holder}
		reader := assemble.CLICardReader{Program: "/build/vibesys-cli", State: state, Runner: world}
		for _, task := range []string{"a", "b", "c"}[:1+r.IntN(3)] {
			card, err := reader.Card(t.Context(), state+"/examples/x", task)
			if err != nil || card.GetTask() != task {
				t.Fatalf("user's daemon at %q: card of %s: %+v, %v", holder, task, card, err)
			}
		}
		if err := reader.Stop(t.Context()); err != nil {
			t.Fatalf("user's daemon at %q: stop: %v", holder, err)
		}
		switch {
		case len(world.reachedHolder) > 0:
			t.Fatalf("user's daemon at %q: the build read through it", holder)
		case world.holderStopped:
			t.Fatalf("user's daemon at %q: the build stopped it", holder)
		case len(world.holdsMachine) > 0:
			t.Fatalf("user's daemon at %q: the build's daemons %v hold the machine lock", holder, world.holdsMachine)
		case len(world.started) > 0:
			t.Fatalf("user's daemon at %q: the build left daemons %v running", holder, world.started)
		}
	}
}

// The environment is the reader's alone, whatever the builder's is: exactly these four variables, all
// under its state folder, a sandbox daemon, and a runtime directory that is absolute and of its own.
// Nothing the person who builds has set (a runtime directory, a sandbox flag, a daemon to reach) is
// passed on, so a shell that was set up to keep one tool from another cannot change what the build reads.
func TestTheCardReadersEnvironmentIsItsStateFolderAlone(t *testing.T) {
	for _, name := range []string{"XDG_RUNTIME_DIR", "VIBESYS_CLI_RUNTIME_DIR", "VIBESYS_CLI_SANDBOX", "XDG_STATE_HOME", "XDG_CACHE_HOME"} {
		t.Setenv(name, "/operator/"+name)
	}
	state := "/tmp/vsb123"
	got := assemble.CLICardReader{Program: "/build/vibesys-cli", State: state}.Environ()
	want := []string{"HOME=" + state, "TMPDIR=" + state, "XDG_RUNTIME_DIR=" + state + "/run", "VIBESYS_CLI_SANDBOX=1"}
	slices.Sort(got)
	slices.Sort(want)
	if !slices.Equal(got, want) {
		t.Fatalf("environment %v, want %v", got, want)
	}
}

// A failure of the program is the reader's error, naming the program, the command and what it said.
func TestACardReadThatFailsSaysWhatTheToolSaid(t *testing.T) {
	// A user's daemon that is in the reader's own runtime directory: the tool refuses a sandbox daemon
	// there, and its words reach the build's error.
	reader := assemble.CLICardReader{Program: "/build/vibesys-cli", State: "/tmp/vsb1", Runner: &machineWorld{uid: 501, holder: "/tmp/vsb1/run/vibesys-cli"}}
	_, err := reader.Card(t.Context(), "/tmp/vsb1/examples/x", "a")
	if err == nil || !strings.Contains(err.Error(), "vibesys-cli task card") || !strings.Contains(err.Error(), "holds this machine") {
		t.Fatalf("got %v, want the tool's own words", err)
	}
}
