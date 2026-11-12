package host_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/shorttmp"
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
	os.Exit(shorttmp.Run(m))
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
	records  string
	boot     string
	gates    *fakeenv.FIFOGates
	lifeline *os.File
	h        *host.Host
}

func (r *hostRig) newHost() *host.Host {
	exe, err := os.Executable()
	if err != nil {
		r.t.Fatal(err)
	}
	return host.New(host.Config{
		Root: r.root, Records: r.records,
		BootID: func() (string, error) { return r.boot, nil },
		Command: func(dir string) *exec.Cmd {
			cmd := exec.Command(exe, dir)
			cmd.Env = append(os.Environ(), roleVar+"=supervisor", gatesVar+"="+r.gates.Dir())
			cmd.ExtraFiles = []*os.File{r.lifeline}
			return cmd
		},
		BinaryVersion: "test", Timers: clock.System{},
	})
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
	r := &hostRig{t: t, root: root, records: t.TempDir(), boot: "first", gates: fakeenv.NewFIFOGates(gatesDir), lifeline: lifelineR}
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
			Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"print", strings.Repeat("x", 4<<20), ";", "exit", "0"}, Timeout: time.Hour},
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
	found, err := next.Probe(ctx, l.Ref())
	run, alive := found.(supervisor.Alive)
	if err != nil || !alive {
		t.Fatalf("Probe = %+v, %v", found, err)
	}
	defer run.Handle.Close()
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

// Only proof of death lets a run go. A supervisor that cannot be reached, here because its socket
// is gone and for one of another build because the handshake refuses, may have a command running,
// so it stays until its lock says otherwise. A done context says nothing about it at all.
func TestOnlyProofOfDeathLetsARunGo(t *testing.T) {
	r := openHostRig(t)
	ctx, cancel := context.WithTimeout(context.Background(), suptest.Backstop)
	defer cancel()
	l := lifecycle.Launch{
		Repository: "/repo", Task: "task", Job: "unreachable",
		Spec: core.JobSpec{
			Commit: "0123456789abcdef0123456789abcdef01234567", Environment: core.EnvironmentHost,
			Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"exit", "0"}, Timeout: time.Hour},
		},
	}
	if _, err := r.h.Spawn(ctx, l); err != nil {
		t.Fatal(err)
	}
	infos, err := host.Inspect(r.root)
	if err != nil || len(infos) != 1 {
		t.Fatalf("Inspect = %+v, %v", infos, err)
	}
	if err := os.Remove(infos[0].Socket); err != nil {
		t.Fatal(err)
	}
	next := r.newHost()
	ref := l.Ref()
	stillThere := func(when string) {
		t.Helper()
		if left, err := host.Inspect(r.root); err != nil || len(left) != 1 {
			t.Fatalf("%s: run directories = %+v, %v", when, left, err)
		}
	}

	found, err := next.Probe(ctx, ref)
	unknown, ok := found.(supervisor.Unknown)
	if err != nil || !ok || unknown.Launch == nil || unknown.Launch.Ref() != ref {
		t.Fatalf("Probe = %+v, %v; want an unknown run with its launch", found, err)
	}
	var refused *core.Failure
	if err := next.Forget(ctx, ref, lifecycle.ProofLockFree); !errors.As(err, &refused) || refused.Kind != core.FailureConflict {
		t.Fatalf("Forget of a run that may live = %v, want a conflict", err)
	}
	stillThere("after Forget")

	done, stop := context.WithCancel(ctx)
	stop()
	if _, err := next.Probe(done, ref); !errors.Is(err, context.Canceled) {
		t.Fatalf("Probe with a done context = %v", err)
	}
	if _, err := next.Runs(done); !errors.Is(err, context.Canceled) {
		t.Fatalf("Runs with a done context = %v", err)
	}
	if err := next.Forget(done, ref, lifecycle.ProofLockFree); !errors.Is(err, context.Canceled) {
		t.Fatalf("Forget with a done context = %v", err)
	}
	stillThere("with a done context")

	if err := syscall.Kill(infos[0].PID, syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	gone, err := runtimedir.WaitLock(ctx, infos[0].Alive)
	if err != nil {
		t.Fatal(err)
	}
	gone.Unlock()
	found, err = next.Probe(ctx, ref)
	dead, ok := found.(supervisor.Dead)
	if err != nil || !ok {
		t.Fatalf("Probe of a dead supervisor = %+v, %v", found, err)
	}
	if err := next.Forget(ctx, ref, dead.Proof); err != nil {
		t.Fatal(err)
	}
	if left, err := host.Inspect(r.root); err != nil || len(left) != 0 {
		t.Fatalf("after Forget of a dead run: %+v, %v", left, err)
	}
}

// A runtime directory can go while the machine stays up, as it does at logout, and the supervisor
// goes on. Its absence is no proof of anything then, and it is only one when the machine restarted.
func TestAMissingRunDirectoryProvesNothingUntilAReboot(t *testing.T) {
	r := openHostRig(t)
	ctx, cancel := context.WithTimeout(context.Background(), suptest.Backstop)
	defer cancel()
	l := lifecycle.Launch{
		Repository: "/repo", Task: "task", Job: "wiped",
		Spec: core.JobSpec{
			Commit: "0123456789abcdef0123456789abcdef01234567", Environment: core.EnvironmentHost,
			Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"exit", "0"}, Timeout: time.Hour},
		},
	}
	h, err := r.h.Spawn(ctx, l)
	if err != nil {
		t.Fatal(err)
	}
	defer h.Close()
	ref := l.Ref()
	if err := os.RemoveAll(filepath.Join(r.root, ref.String())); err != nil {
		t.Fatal(err)
	}

	next := r.newHost()
	found, err := next.Probe(ctx, ref)
	unknown, ok := found.(supervisor.Unknown)
	if err != nil || !ok || unknown.Launch == nil || unknown.Launch.Ref() != ref {
		t.Fatalf("Probe in the same boot = %#v, %v; want an unknown run with its launch", found, err)
	}
	runs, err := next.Runs(ctx)
	if err != nil || len(runs) != 1 {
		t.Fatalf("Runs in the same boot = %#v, %v; want the run", runs, err)
	}

	r.boot = "second"
	found, err = next.Probe(ctx, ref)
	dead, ok := found.(supervisor.Dead)
	if err != nil || !ok || dead.Proof != lifecycle.ProofRebooted || dead.Launch == nil {
		t.Fatalf("Probe after a reboot = %#v, %v; want proof of the reboot", found, err)
	}
	if err := next.Forget(ctx, ref, dead.Proof); err != nil {
		t.Fatal(err)
	}
	if found, err := next.Probe(ctx, ref); err != nil {
		t.Fatal(err)
	} else if _, none := found.(supervisor.None); !none {
		t.Fatalf("Probe after Forget = %#v, want none", found)
	}
}

// A job with no record never had a run, whatever the boot.
func TestAJobWithNoRecordHasNoRun(t *testing.T) {
	r := openHostRig(t)
	ctx, cancel := context.WithTimeout(context.Background(), suptest.Backstop)
	defer cancel()
	found, err := r.h.Probe(ctx, lifecycle.Ref{Store: "s", Job: "never"})
	if _, none := found.(supervisor.None); err != nil || !none {
		t.Fatalf("Probe = %#v, %v; want none", found, err)
	}
}

// A run record written by a build that did not mark starts says nothing of the start, so its
// command may have started: an upgrade over a run of the old build, after a reboot, must not run
// its command twice.
func TestARecordFromBeforeStartMarksMayHaveStarted(t *testing.T) {
	r := openHostRig(t)
	ctx, cancel := context.WithTimeout(context.Background(), suptest.Backstop)
	defer cancel()
	l := lifecycle.Launch{
		Repository: "/repo", Task: "task", Job: "old",
		Spec: core.JobSpec{
			Commit: "0123456789abcdef0123456789abcdef01234567", Environment: core.EnvironmentHost,
			Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"exit", "0"}, Timeout: time.Hour},
		},
	}
	h, err := r.h.Spawn(ctx, l)
	if err != nil {
		t.Fatal(err)
	}
	defer h.Close()
	ref := l.Ref()
	writeOldRecord(t, filepath.Join(r.records, ref.String()))
	if err := os.RemoveAll(filepath.Join(r.root, ref.String())); err != nil {
		t.Fatal(err)
	}
	r.boot = "second"

	found, err := r.newHost().Probe(ctx, ref)
	dead, ok := found.(supervisor.Dead)
	if err != nil || !ok || dead.Proof != lifecycle.ProofRebooted {
		t.Fatalf("Probe after a reboot = %#v, %v; want proof of the reboot", found, err)
	}
	if _, may := dead.Start.(lifecycle.MayHaveStarted); !may {
		t.Fatalf("Probe after a reboot says the start was %#v; a record from before start marks proves nothing of it", dead.Start)
	}
}

// writeOldRecord rewrites a run record in the shape a build before start marks wrote: the boot and
// the launch, and nothing else.
func writeOldRecord(t *testing.T, path string) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var rec cliv1.RunRecord
	if err := proto.Unmarshal(data, &rec); err != nil {
		t.Fatal(err)
	}
	old, err := proto.Marshal(&cliv1.RunRecord{BootId: rec.GetBootId(), Launch: rec.GetLaunch()})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, old, 0o600); err != nil {
		t.Fatal(err)
	}
}
