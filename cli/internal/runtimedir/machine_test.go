package runtimedir_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// free is a context for a claim of a lock that is free, or let go of by a holder: nothing in the
// test holds it, so the claim takes it at its first try.
func free(t *testing.T) context.Context { return t.Context() }

// held is a context for a claim of a lock that is held: it is done, so the claim tries once and
// sees the lock held.
func held(t *testing.T) context.Context {
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	return ctx
}

// noWait is the timers of a claim: a test never waits between tries.
var noWait = clock.Immediate{}

func openMachine(t *testing.T) runtimedir.Machine {
	t.Helper()
	m, err := runtimedir.OpenMachine(filepath.Join(shortDir(t), "machine"), os.Getuid(), fakeFS{})
	if err != nil {
		t.Fatal(err)
	}
	return m
}

// The machine directory comes from the user id and the machine, and no variable moves it:
// /var/tmp when it is local, the user's /run/user directory when /var/tmp is not, and none when
// neither is there.
func TestTheMachineDirectoryIsTheUsersOnLocalDisk(t *testing.T) {
	root := shortDir(t)
	varTmp, runUser := filepath.Join(root, "var-tmp"), filepath.Join(root, "run-user")
	got, err := runtimedir.ResolveMachineIn(7, fakeFS{}, varTmp, runUser)
	if err != nil || got != filepath.Join(varTmp, "vibesys-cli-7") {
		t.Fatalf("with a local /var/tmp: %q, %v", got, err)
	}
	_, err = runtimedir.ResolveMachineIn(7, fakeFS{network: true}, varTmp, runUser)
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, "login session") {
		t.Fatalf("with a network /var/tmp and no /run/user/7: %v", err)
	}
	if err := os.MkdirAll(filepath.Join(runUser, "7"), 0o700); err != nil {
		t.Fatal(err)
	}
	got, err = runtimedir.ResolveMachineIn(7, fakeFS{network: true}, varTmp, runUser)
	if err != nil || got != filepath.Join(runUser, "7", "vibesys-cli-machine") {
		t.Fatalf("with a network /var/tmp and /run/user/7: %q, %v", got, err)
	}
	_, err = runtimedir.OpenMachine(filepath.Join(root, "machine"), os.Getuid(), fakeFS{network: true})
	if f := core.AsFailure(err); err == nil || f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, "network file system") {
		t.Fatalf("a machine directory on a network file system: %v", err)
	}
}

// One daemon holds the machine: a second is refused, naming the first, until the first lets go.
func TestASecondDaemonIsRefusedNamingTheHolder(t *testing.T) {
	m := openMachine(t)
	first := &cliv1.MachineDaemon{Pid: 41, RuntimeDir: "/run/user/7/vibesys-cli", Version: "1.2.3"}
	c, err := m.Claim(free(t), noWait, first)
	if err != nil {
		t.Fatal(err)
	}
	if h, err := m.Holder(); err != nil || !proto.Equal(h, first) {
		t.Fatalf("the holder is %v, %v; want %v", h, err, first)
	}
	_, err = m.Claim(held(t), noWait, &cliv1.MachineDaemon{Pid: 42, RuntimeDir: "/tmp/other/vibesys-cli", Version: "1.2.3"})
	var h *runtimedir.HeldError
	if !errors.As(err, &h) || !proto.Equal(h.Holder, first) {
		t.Fatalf("a second daemon: %v; want a HeldError naming %v", err, first)
	}
	for _, want := range []string{"pid 41", "/run/user/7/vibesys-cli", "version 1.2.3", "VIBESYS_CLI_SANDBOX"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal %q does not say %q", err, want)
		}
	}
	if err := c.Release(); err != nil {
		t.Fatal(err)
	}
	if h, err := m.Holder(); err != nil || h != nil {
		t.Fatalf("after the release the holder is %v, %v", h, err)
	}
	second := &cliv1.MachineDaemon{Pid: 42, RuntimeDir: "/tmp/other/vibesys-cli", Version: "1.2.4"}
	c, err = m.Claim(free(t), noWait, second)
	if err != nil {
		t.Fatalf("the machine let go of: %v", err)
	}
	if h, _ := m.Holder(); !proto.Equal(h, second) {
		t.Fatalf("the new holder is recorded as %v; want %v", h, second)
	}
	_ = c.Release()
}

// A sandbox daemon is listed while it runs, and only then, each by its runtime directory; a second
// one in the same runtime directory is refused.
func TestSandboxDaemonsAreListedWhileTheyRun(t *testing.T) {
	m := openMachine(t)
	a := &cliv1.MachineDaemon{Pid: 50, RuntimeDir: "/tmp/a/vibesys-cli", Version: "v", Sandbox: true}
	b := &cliv1.MachineDaemon{Pid: 51, RuntimeDir: "/tmp/b/vibesys-cli", Version: "v", Sandbox: true}
	ca, err := m.RegisterSandbox(free(t), noWait, a)
	if err != nil {
		t.Fatal(err)
	}
	cb, err := m.RegisterSandbox(free(t), noWait, b)
	if err != nil {
		t.Fatal(err)
	}
	list, err := m.Sandboxes()
	if err != nil || len(list) != 2 || !proto.Equal(list[0], a) || !proto.Equal(list[1], b) {
		t.Fatalf("sandboxes %v, %v; want a and b", list, err)
	}
	var h *runtimedir.HeldError
	if _, err := m.RegisterSandbox(held(t), noWait, &cliv1.MachineDaemon{Pid: 52, RuntimeDir: a.RuntimeDir, Version: "v", Sandbox: true}); !errors.As(err, &h) || !proto.Equal(h.Holder, a) {
		t.Fatalf("a second sandbox in a's runtime directory: %v", err)
	}
	if _, err := m.RegisterSandbox(free(t), noWait, &cliv1.MachineDaemon{Pid: 53, RuntimeDir: "/tmp/c", Version: "v"}); err == nil {
		t.Fatal("a daemon that is not a sandbox was registered as one")
	}
	if err := ca.Release(); err != nil {
		t.Fatal(err)
	}
	if list, err := m.Sandboxes(); err != nil || len(list) != 1 || !proto.Equal(list[0], b) {
		t.Fatalf("after a let go, sandboxes %v, %v; want b", list, err)
	}
	// A daemon that ended without letting go leaves its file, with its lock free: it is not listed.
	if err := cb.Release(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(m.Path(), "sandboxes", "dead.lock"), []byte(`{"pid": 9, "runtimeDir": "/x", "sandbox": true}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if list, err := m.Sandboxes(); err != nil || len(list) != 0 {
		t.Fatalf("with no sandbox running, sandboxes %v, %v", list, err)
	}
}
