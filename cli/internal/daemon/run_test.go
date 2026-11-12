package daemon_test

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// process is a Run on a real runtime directory, in this process.
type process struct {
	t        *testing.T
	dir      runtimedir.Dir
	survive  *supervisor.Survivors
	done     chan error
	ready    *os.File
	readyEnd *os.File
}

type noFS struct{}

func (noFS) IsNetwork(string) (bool, error) { return false, nil }

func openDir(t *testing.T) runtimedir.Dir {
	t.Helper()
	// A short path: a socket address is limited to about a hundred bytes.
	root, err := os.MkdirTemp("", "vsd")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	dir, err := runtimedir.Open(filepath.Join(root, "rt"), os.Getuid(), noFS{})
	if err != nil {
		t.Fatal(err)
	}
	return dir
}

func runtimeOf(t *testing.T, survivors *supervisor.Survivors) daemon.Runtime {
	env := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates())
	return daemon.Runtime{
		Stores:       memstores.New(app.Rules(5)),
		Supervisors:  supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: env}, &fakeenv.Checkouts{}, survivors),
		Environments: fakeenv.Environments{core.EnvironmentHost: env},
		Repositories: repos(),
	}
}

// start runs a daemon and returns once it says it serves, or fails the test if it does not.
func start(t *testing.T, ctx context.Context, dir runtimedir.Dir) (*process, error) {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	p := &process{t: t, dir: dir, survive: supervisor.NewSurvivors(), done: make(chan error, 1), ready: r, readyEnd: w}
	t.Cleanup(func() { r.Close(); p.survive.Close() })
	go func() {
		p.done <- daemon.Run(ctx, daemon.Config{
			Dir: dir, Version: "test", Runtime: runtimeOf(t, p.survive),
			Clock: clock.System{}, Ready: w,
		})
	}()
	buf := make([]byte, 1)
	if _, err := io.ReadFull(r, buf); err != nil {
		// The pipe closed without the byte: the daemon failed before it served.
		return nil, <-p.done
	}
	switch buf[0] {
	case runtimedir.ReadyByte:
		return p, nil
	case runtimedir.ReadyTaken:
		return nil, <-p.done
	}
	t.Fatalf("the daemon wrote %q to the readiness pipe", buf)
	return nil, nil
}

func dial(t *testing.T, dir runtimedir.Dir) cliv1.DaemonServiceClient {
	t.Helper()
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", dir.Socket())
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return cliv1.NewDaemonServiceClient(conn)
}

// A daemon that stopped reading nothing but whose client did cannot be kept from stopping: the
// machine is held for as long as a daemon runs. The stop waits, and then it does not.
func TestAClientThatStoppedReadingDoesNotHoldAStoppingDaemon(t *testing.T) {
	dir := openDir(t)
	ctx := ctxFor(t)
	timers := clock.NewManualTimers()
	survivors := supervisor.NewSurvivors()
	t.Cleanup(survivors.Close)
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	done := make(chan error, 1)
	go func() {
		done <- daemon.Run(ctx, daemon.Config{
			Dir: dir, Version: "test", Runtime: runtimeOf(t, survivors), Clock: clock.System{}, Timers: timers, Ready: w,
		})
	}()
	if _, err := io.ReadFull(r, make([]byte, 1)); err != nil {
		t.Fatal(err)
	}
	c := dial(t, dir)
	call := &cliv1.CallContext{Repository: "/repo", Task: "task", RunId: "run", Harness: "h", Agent: "a"}
	sub, err := c.Submit(ctx, &cliv1.SubmitRequest{Context: call, Spec: &cliv1.JobSpec{
		Commit: "0123456789abcdef0123456789abcdef01234567", Environment: cliv1.EnvironmentKind_ENVIRONMENT_KIND_HOST,
		Settings: &cliv1.JobSpec_Command{Command: &cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_HARNESS, Argv: []string{"print", strings.Repeat("x", 3<<20), ";", "exit", "0"}}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	// The whole output is stored before anyone reads it, so the daemon has more to send than the
	// stream holds.
	watch, err := c.Watch(ctx, &cliv1.WatchRequest{Context: call, JobIds: []string{sub.GetJob().GetId()}})
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := watch.Recv(); errors.Is(err, io.EOF) {
			break
		} else if err != nil {
			t.Fatal(err)
		}
	}
	logs, err := c.ReadLogs(ctx, &cliv1.ReadLogsRequest{Context: call, JobId: sub.GetJob().GetId()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := logs.Recv(); err != nil { // The stream is up, and this client reads no more of it.
		t.Fatal(err)
	}
	if _, err := c.Stop(ctx, &cliv1.StopRequest{}); err != nil {
		t.Fatal(err)
	}
	for {
		select {
		case <-timers.Armed():
			timers.Fire()
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
			return
		case <-ctx.Done():
			t.Fatal("the daemon did not stop")
		}
	}
}

func TestRunServesTheSocketPrivatelyAndStopsOnStop(t *testing.T) {
	dir := openDir(t)
	p, err := start(t, ctxFor(t), dir)
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(dir.Socket())
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("socket mode = %o, want 600", perm)
	}
	client := dial(t, dir)
	if _, err := client.Hello(ctxFor(t), &cliv1.HelloRequest{
		Component: cliv1.Component_COMPONENT_CLI, Versions: wire.BuildVersions("test", core.CurrentVersions())}); err != nil {
		t.Fatalf("Hello: %v", err)
	}
	if _, err := client.Stop(ctxFor(t), &cliv1.StopRequest{}); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if err := <-p.done; err != nil {
		t.Fatalf("Run = %v after Stop, want nil", err)
	}
	if _, err := os.Stat(dir.Socket()); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("the socket is still there after the daemon stopped (stat error %v)", err)
	}
	// The lock goes with the process: a new daemon can take it at once.
	lock, err := runtimedir.TryLock(dir.DaemonLock())
	if err != nil {
		t.Fatalf("the instance lock is still held: %v", err)
	}
	lock.Unlock()
}

// lockWatchingStores says, when it is closed, whether the instance lock was still held.
type lockWatchingStores struct {
	app.Stores
	dir  runtimedir.Dir
	seen chan error
}

func (s lockWatchingStores) Close() error {
	lock, err := runtimedir.TryLock(s.dir.DaemonLock())
	if err == nil {
		lock.Unlock()
	}
	s.seen <- err
	return s.Stores.Close()
}

// A free instance lock tells a client the daemon is gone, and the next daemon must find the stores
// free. So however a daemon ends, the stores go first.
func TestADaemonGivesUpItsStoresBeforeItLetsGoOfTheMachine(t *testing.T) {
	cases := map[string]struct {
		stores app.Stores
		timers clock.Timers
		stop   bool
	}{
		"after serving":          {stores: memstores.New(app.Rules(5)), timers: clock.System{}, stop: true},
		"after failing to start": {stores: brokenStores{}, timers: clock.Immediate{Longest: time.Minute}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			dir := openDir(t)
			survivors := supervisor.NewSurvivors()
			defer survivors.Close()
			watching := lockWatchingStores{Stores: tc.stores, dir: dir, seen: make(chan error, 1)}
			r, w, err := os.Pipe()
			if err != nil {
				t.Fatal(err)
			}
			defer r.Close()
			rt := runtimeOf(t, survivors)
			rt.Stores = watching
			ctx := ctxFor(t)
			done := make(chan error, 1)
			go func() {
				done <- daemon.Run(ctx, daemon.Config{
					Dir: dir, Version: "test", Runtime: rt, Clock: clock.System{}, Timers: tc.timers, Ready: w,
				})
			}()
			if tc.stop {
				if _, err := io.ReadFull(r, make([]byte, 1)); err != nil {
					t.Fatal(err)
				}
				if _, err := dial(t, dir).Stop(ctxFor(t), &cliv1.StopRequest{}); err != nil {
					t.Fatal(err)
				}
			}
			// Run closes the stores synchronously. Join it before checking: selecting
			// between done and seen can choose done even when both are ready.
			var runErr error
			select {
			case runErr = <-done:
			case <-ctx.Done():
				t.Fatal("the daemon did not stop")
			}
			select {
			case err := <-watching.seen:
				if !errors.Is(err, runtimedir.ErrHeld) {
					t.Fatalf("the stores closed with the instance lock free (taking it gave %v)", err)
				}
			default:
				t.Fatalf("Run ended (%v) and never closed the stores", runErr)
			}
		})
	}
}

// A second daemon of the user does not start, and does not disturb the first.
func TestASecondDaemonIsRefusedAndTheFirstIsUntouched(t *testing.T) {
	dir := openDir(t)
	ctx, stop := context.WithCancel(ctxFor(t))
	p, err := start(t, ctx, dir)
	if err != nil {
		t.Fatal(err)
	}
	_, err = start(t, ctxFor(t), dir)
	if !errors.Is(err, daemon.ErrAlreadyRunning) {
		t.Fatalf("second Run = %v, want ErrAlreadyRunning", err)
	}
	// The first still answers: the second did not remove its socket.
	if _, err := dial(t, dir).Status(ctxFor(t), &cliv1.StatusRequest{}); err != nil {
		t.Fatalf("the first daemon stopped answering: %v", err)
	}
	stop()
	if err := <-p.done; err != nil {
		t.Fatal(err)
	}
}

// A socket left by a daemon that died is replaced, not an obstacle.
func TestAStaleSocketIsReplaced(t *testing.T) {
	dir := openDir(t)
	stale, err := net.Listen("unix", dir.Socket())
	if err != nil {
		t.Fatal(err)
	}
	// Close the listener without removing the file, as a crashed process leaves it.
	stale.(*net.UnixListener).SetUnlinkOnClose(false)
	stale.Close()

	ctx, stop := context.WithCancel(ctxFor(t))
	p, err := start(t, ctx, dir)
	if err != nil {
		t.Fatalf("Run with a stale socket: %v", err)
	}
	if _, err := dial(t, dir).Status(ctxFor(t), &cliv1.StatusRequest{}); err != nil {
		t.Fatal(err)
	}
	stop()
	if err := <-p.done; err != nil {
		t.Fatal(err)
	}
}

// A daemon that cannot recover does not serve, and says so through the readiness pipe.
func TestADaemonThatCannotRecoverDoesNotServe(t *testing.T) {
	dir := openDir(t)
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	err = daemon.Run(ctxFor(t), daemon.Config{
		Dir: dir, Version: "test", Clock: clock.System{}, Timers: clock.Immediate{Longest: time.Minute}, Ready: w,
		Runtime: daemon.Runtime{
			Stores:       brokenStores{},
			Supervisors:  supervisor.NewInProcess(fakeenv.Environments{}, &fakeenv.Checkouts{}, supervisor.NewSurvivors()),
			Environments: environment.None{},
		},
	})
	if err == nil {
		t.Fatal("Run succeeded over stores that cannot be listed")
	}
	if n, _ := r.Read(make([]byte, 1)); n != 0 {
		t.Fatal("the daemon said it served")
	}
	if _, err := os.Stat(dir.Socket()); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("a daemon that failed left a socket (stat error %v)", err)
	}
}

type brokenStores struct{ app.Stores }

func (brokenStores) Known(context.Context) ([]core.Scope, error) {
	return nil, &core.Failure{Kind: core.FailureUnavailable, Message: "the disk is gone"}
}

func (brokenStores) Close() error { return nil }

func mustScope(t *testing.T, repo, task string) core.Scope {
	t.Helper()
	s, err := core.ResolvedScope(core.Repository(repo), core.TaskName(task))
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// fakeMachine is a daemon.Machine: Claim fails with refusal when it is set, and otherwise counts
// how often the claim is held.
type fakeMachine struct {
	refusal   error
	sandbox   bool
	sandboxes []*cliv1.MachineDaemon
	held      *int
}

func (m fakeMachine) Claim(context.Context) (func() error, error) {
	if m.refusal != nil {
		return nil, m.refusal
	}
	*m.held++
	return func() error { *m.held--; return nil }, nil
}
func (m fakeMachine) Sandbox() bool                              { return m.sandbox }
func (m fakeMachine) Sandboxes() ([]*cliv1.MachineDaemon, error) { return m.sandboxes, nil }

// A daemon that another daemon's machine lock refuses does not serve, says why, and leaves its
// runtime directory free; one that takes the machine holds it until it stops.
func TestADaemonTheMachineRefusesDoesNotServe(t *testing.T) {
	dir := openDir(t)
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	refusal := &runtimedir.HeldError{Path: "/m/machine.lock", Holder: &cliv1.MachineDaemon{Pid: 7, RuntimeDir: "/run/a", Version: "v"}}
	err = daemon.Run(ctxFor(t), daemon.Config{
		Dir: dir, Version: "test", Clock: clock.System{}, Ready: w, Runtime: runtimeOf(t, supervisor.NewSurvivors()),
		Machine: fakeMachine{refusal: refusal},
	})
	var held *runtimedir.HeldError
	if !errors.As(err, &held) || !strings.Contains(err.Error(), "pid 7") {
		t.Fatalf("Run = %v; want the machine's refusal", err)
	}
	if n, _ := r.Read(make([]byte, 1)); n != 0 {
		t.Fatal("the refused daemon said it served")
	}
	lock, err := runtimedir.TryLock(dir.DaemonLock())
	if err != nil {
		t.Fatalf("the refused daemon kept its runtime directory: %v", err)
	}
	_ = lock.Unlock()
}

// The status says whether the daemon is a sandbox, and lists the sandbox daemons that run.
func TestTheStatusListsTheSandboxDaemons(t *testing.T) {
	a := app.New(app.Config{Clock: clock.System{}, Stores: memstores.New(app.Rules(5)), Environments: environment.None{},
		Supervisors: supervisor.NewInProcess(fakeenv.Environments{}, &fakeenv.Checkouts{}, supervisor.NewSurvivors())})
	if err := a.Start(ctxFor(t)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.Close)
	sb := &cliv1.MachineDaemon{Pid: 9, RuntimeDir: "/tmp/s/vibesys-cli", Version: "v", Sandbox: true}
	held := 0
	st, err := daemon.NewServer(a, "test", daemon.Inherited{}).WithMachine(fakeMachine{sandbox: true, sandboxes: []*cliv1.MachineDaemon{sb}, held: &held}).Status(ctxFor(t), &cliv1.StatusRequest{})
	if err != nil || !st.GetSandbox() || len(st.GetSandboxes()) != 1 || st.GetSandboxes()[0].GetPid() != 9 {
		t.Fatalf("status %v, %v", st, err)
	}
	st, err = daemon.NewServer(a, "test", daemon.Inherited{}).Status(ctxFor(t), &cliv1.StatusRequest{})
	if err != nil || st.GetSandbox() || len(st.GetSandboxes()) != 0 {
		t.Fatalf("status with no machine %v, %v", st, err)
	}
}
