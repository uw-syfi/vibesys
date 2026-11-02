package host_test

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
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
	}, "test", clock.System{})
}

func newHostRig(t *testing.T) suptest.Rig {
	t.Helper()
	return openHostRig(t).rig()
}

func openHostRig(t *testing.T) *hostRig {
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
	return r
}

func (r *hostRig) rig() suptest.Rig {
	t := r.t
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

// A caller that stopped reading, like a daemon that was frozen, cannot keep a released supervisor
// alive: the process goes, and with it what it holds. The Release goes through a second connection,
// as a new daemon's does.
func TestAReleasedSupervisorDoesNotWaitForACallerThatStoppedReading(t *testing.T) {
	r := openHostRig(t)
	ctx, cancel := context.WithTimeout(context.Background(), suptest.Backstop)
	defer cancel()
	l := lifecycle.Launch{
		Repository: "/repo", Task: "task", Job: "stalled",
		Spec: core.JobSpec{
			Commit: "0123456789abcdef0123456789abcdef01234567", Environment: core.EnvironmentHost,
			Settings: core.CommandSettings{Argv: []string{"print", strings.Repeat("x", 4<<20), ";", "exit", "0"}, Timeout: time.Hour},
		},
	}
	frozen, err := r.h.Spawn(ctx, l)
	if err != nil {
		t.Fatal(err)
	}
	if err := frozen.Start(ctx); err != nil {
		t.Fatal(err)
	}
	stalled, unstall := make(chan struct{}), make(chan struct{})
	var follower sync.WaitGroup
	follower.Go(func() {
		var once sync.Once
		frozen.Follow(ctx, 0, func([]byte) error {
			once.Do(func() { close(stalled) })
			<-unstall
			return nil
		})
	})
	defer follower.Wait()
	defer close(unstall)
	<-stalled

	next := r.newHost()
	run, err := next.Probe(ctx, l.Ref())
	if err != nil || run.Handle == nil {
		t.Fatalf("Probe = %+v, %v", run, err)
	}
	if _, err := run.Handle.Follow(ctx, 1<<62, func([]byte) error { return nil }); err != nil {
		t.Fatal(err)
	}
	infos, err := host.Inspect(r.root)
	if err != nil || len(infos) != 1 {
		t.Fatalf("Inspect = %+v, %v", infos, err)
	}
	if err := run.Handle.Release(ctx); err != nil {
		t.Fatal(err)
	}
	gone, err := runtimedir.WaitLock(ctx, infos[0].Alive)
	if err != nil {
		t.Fatalf("the released supervisor is still there: %v", err)
	}
	gone.Unlock()
}
