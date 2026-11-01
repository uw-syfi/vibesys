package host_test

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/host"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/suptest"
)

// The test binary is also the supervisor: Host starts it again with this variable set, and it
// serves the run directory it is given. That makes the processes real and the command a fake.
const (
	roleVar  = "VIBESYS_HOST_TEST_ROLE"
	gatesVar = "VIBESYS_HOST_TEST_GATES"
	// lifelineFD is the end of a pipe the test holds open. A supervisor that finds it closed, because
	// the test process died, ends its command and exits, so a crashed test leaves nothing running.
	lifelineFD = host.ReadyFD + 1
)

func TestMain(m *testing.M) {
	if os.Getenv(roleVar) == "supervisor" {
		os.Exit(superviseForTest())
	}
	os.Exit(m.Run())
}

func superviseForTest() int {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	lifeline := os.NewFile(lifelineFD, "lifeline")
	go func() {
		var b [1]byte
		lifeline.Read(b[:])
		stop()
	}()
	deps := host.Deps{
		Environments: fakeenv.Environments{
			core.EnvironmentHost: fakeenv.New(core.EnvironmentHost, fakeenv.NewFIFOGates(os.Getenv(gatesVar))),
		},
		Checkouts:     &fakeenv.Checkouts{},
		BinaryVersion: "test",
	}
	if err := host.Serve(ctx, os.Args[1], deps, os.NewFile(host.ReadyFD, "ready")); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

type hostRig struct {
	t        *testing.T
	root     string
	gates    *fakeenv.FIFOGates
	lifeline *os.File
	h        *host.Host
}

func (r *hostRig) newHost() *host.Host {
	exe, err := os.Executable()
	if err != nil {
		r.t.Fatal(err)
	}
	return host.New(r.root, func(dir string) *exec.Cmd {
		cmd := exec.Command(exe, dir)
		cmd.Env = append(os.Environ(), roleVar+"=supervisor", gatesVar+"="+r.gates.Dir())
		cmd.ExtraFiles = []*os.File{r.lifeline}
		return cmd
	}, "test")
}

func newHostRig(t *testing.T) suptest.Rig {
	t.Helper()
	// A short directory: a Unix socket path is limited to about a hundred bytes.
	root, err := os.MkdirTemp("", "vsh")
	if err != nil {
		t.Fatal(err)
	}
	gatesDir, err := os.MkdirTemp("", "vsg")
	if err != nil {
		t.Fatal(err)
	}
	lifelineR, lifelineW, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	r := &hostRig{t: t, root: root, gates: fakeenv.NewFIFOGates(gatesDir), lifeline: lifelineR}
	r.h = r.newHost()
	t.Cleanup(func() {
		// Closing the pipe's write end ends every supervisor still up, with its command.
		lifelineW.Close()
		lifelineR.Close()
		os.RemoveAll(root)
		os.RemoveAll(gatesDir)
	})
	return suptest.Rig{
		Supervisors: r.h,
		Restart:     func() supervisor.Supervisors { return r.newHost() },
		Open: func(gate string) {
			if err := r.gates.Release(gate); err != nil {
				t.Errorf("opening gate %s: %v", gate, err)
			}
		},
		Kill: func(ref lifecycle.Ref) {
			pid, err := r.h.PID(ref)
			if err != nil {
				t.Errorf("no pid for %s: %v", ref, err)
				return
			}
			syscall.Kill(pid, syscall.SIGKILL)
		},
	}
}

func TestHostPassesTheContract(t *testing.T) {
	suptest.Run(t, newHostRig)
}
