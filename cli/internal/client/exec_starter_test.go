package client_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// The test binary is also the daemon the ExecStarter starts: when it is run with these variables
// set it does what the variable says and exits, and otherwise it runs the tests.
const (
	childMode = "VIBESYS_CLIENT_TEST_CHILD"
	childDir  = "VIBESYS_CLIENT_TEST_DIR"
)

func TestMain(m *testing.M) {
	switch os.Getenv(childMode) {
	case "daemon":
		os.Exit(runChildDaemon())
	case "fail":
		fmt.Fprintln(os.Stderr, "cannot start: the disk is gone")
		os.Exit(3)
	case "hang":
		select {}
	}
	os.Exit(m.Run())
}

type noFS struct{}

func (noFS) IsNetwork(string) (bool, error) { return false, nil }

func runChildDaemon() int {
	dir, err := runtimedir.Open(os.Getenv(childDir), os.Getuid(), noFS{})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	envs := fakeenv.Environments{core.EnvironmentHost: fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates())}
	survivors := supervisor.NewSurvivors()
	defer survivors.Close()
	err = daemon.Run(context.Background(), daemon.Config{
		Dir: dir, Version: "child", Clock: clock.System{}, Ready: os.NewFile(runtimedir.ReadyFD, "ready"),
		Runtime: daemon.Runtime{
			Stores: app.NewMemoryStores(app.Rules(5)), Environments: envs,
			Supervisors: supervisor.NewInProcess(envs, &fakeenv.Checkouts{}, survivors),
		},
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	return 0
}

func execStarter(t *testing.T, dir runtimedir.Dir, mode string) client.ExecStarter {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return client.ExecStarter{Dir: dir, Command: func() *exec.Cmd {
		cmd := exec.Command(self, "-test.run=^$")
		cmd.Env = append(os.Environ(), childMode+"="+mode, childDir+"="+dir.Path())
		return cmd
	}}
}

// killDaemon ends a daemon process left over by a failed test, so none outlives it.
func killDaemon(t *testing.T, dir runtimedir.Dir) {
	t.Helper()
	t.Cleanup(func() {
		c, err := client.Dial(ctxFor(t), client.Options{Rendezvous: client.LocalRendezvous{Dir: dir}, Version: "test"})
		if err != nil {
			return
		}
		defer c.Close()
		if st, err := c.Status(ctxFor(t)); err == nil {
			syscall.Kill(int(st.GetPid()), syscall.SIGKILL)
		}
	})
}

func TestExecStarterStartsADaemonProcessThatOutlivesTheCall(t *testing.T) {
	dir := daemontest.RuntimeDir(t)
	killDaemon(t, dir)
	opts := client.Options{Rendezvous: client.LocalRendezvous{Dir: dir}, Version: "test", Starter: execStarter(t, dir, "daemon")}
	c, err := client.Connect(ctxFor(t), opts)
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()
	st, err := c.Status(ctxFor(t))
	if err != nil {
		t.Fatal(err)
	}
	if int(st.GetPid()) == os.Getpid() {
		t.Fatal("the daemon runs in the client's process")
	}
	// The lock says one daemon runs; stopping it frees the lock and removes the socket.
	if err := c.Stop(ctxFor(t)); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if _, err := os.Stat(dir.Socket()); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("the socket is still there after Stop (stat error %v)", err)
	}
}

// A daemon that dies before it serves fails the start at once, with what it wrote.
func TestExecStarterReportsADaemonThatFailsToStart(t *testing.T) {
	dir := daemontest.RuntimeDir(t)
	err := execStarter(t, dir, "fail").Start(ctxFor(t))
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, "the disk is gone") {
		t.Fatalf("Start = %v, want an unavailable failure carrying the daemon's own message", err)
	}
}

// A start whose context is done ends the daemon it started, so none is left behind.
func TestExecStarterEndsADaemonItGaveUpOn(t *testing.T) {
	dir := daemontest.RuntimeDir(t)
	ctx, cancel := context.WithCancel(ctxFor(t))
	cancel()
	err := execStarter(t, dir, "hang").Start(ctx)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("Start = %v, want context.Canceled", err)
	}
}
