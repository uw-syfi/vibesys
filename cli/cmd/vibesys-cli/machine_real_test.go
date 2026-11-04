package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/production"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// daemonAt is the environment of a command whose daemon is in a runtime directory of its own under
// the test's root, holding the test's own machine directory, a sandbox or not.
func (m *machine) daemonAt(name string, sandbox bool) []string {
	m.t.Helper()
	rt := filepath.Join(m.root, name)
	if err := os.MkdirAll(rt, 0o700); err != nil {
		m.t.Fatal(err)
	}
	flag := ""
	if sandbox {
		flag = "1"
	}
	env := []string{"XDG_RUNTIME_DIR=" + rt, production.EnvSandbox + "=" + flag, machineVar + "=" + filepath.Join(m.root, "machine")}
	m.t.Cleanup(func() { m.cliWith(env, "daemon", "stop") })
	return env
}

// runtimeOf is the runtime directory of a daemon of daemonAt.
func (m *machine) runtimeOf(name string) string { return filepath.Join(m.root, name, "vibesys-cli") }

// pidOf is the pid the daemon of the environment reports.
func (m *machine) pidOf(env []string) int {
	m.t.Helper()
	r := m.cliWith(env, "daemon", "status")
	fields := strings.Fields(r.stdout)
	if r.code != exitOK || len(fields) < 3 || fields[0] != "running" {
		m.t.Fatalf("daemon status = %+v", r)
	}
	pid, err := strconv.Atoi(fields[2])
	if err != nil {
		m.t.Fatalf("daemon status = %+v", r)
	}
	return pid
}

// Two daemons of one user on one machine collide on the machine lock whatever their runtime
// directories: the second is refused, naming the first's pid, runtime directory and version, and it
// starts once the first stops, or dies with no chance to let go.
func TestRealASecondDaemonOfTheUserIsRefusedWhateverItsRuntimeDirectory(t *testing.T) {
	m := newMachine(t)
	a, b := m.daemonAt("rt-a", false), m.daemonAt("rt-b", false)
	if r := m.cliWith(a, "daemon", "ensure"); r.code != exitOK {
		t.Fatalf("the first daemon: %+v", r)
	}
	pid := m.pidOf(a)
	r := m.cliWith(b, "daemon", "ensure")
	if r.code != exitNoAnswer {
		t.Fatalf("a second daemon in another runtime directory: %+v; want it refused", r)
	}
	for _, want := range []string{"holds this machine", fmt.Sprintf("pid %d", pid), m.runtimeOf("rt-a"), "version"} {
		if !strings.Contains(r.stderr, want) {
			t.Errorf("the refusal does not say %q:\n%s", want, r.stderr)
		}
	}

	// A daemon killed with no chance to let go leaves the machine free: the OS lets go of its lock.
	if err := syscall.Kill(pid, syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	gone, err := runtimedir.WaitLock(ctx, filepath.Join(m.runtimeOf("rt-a"), "daemon.lock"))
	if err != nil {
		t.Fatalf("the killed daemon did not end: %v", err)
	}
	_ = gone.Unlock()
	if r := m.cliWith(b, "daemon", "ensure"); r.code != exitOK {
		t.Fatalf("a daemon once the first was killed: %+v", r)
	}
	if r := m.cliWith(a, "daemon", "ensure"); r.code != exitNoAnswer || !strings.Contains(r.stderr, m.runtimeOf("rt-b")) {
		t.Fatalf("the first again, beside the second: %+v; want it refused naming the second", r)
	}
	if r := m.cliWith(b, "daemon", "stop"); r.code != exitOK {
		t.Fatalf("stopping the second: %+v", r)
	}
	if r := m.cliWith(a, "daemon", "ensure"); r.code != exitOK {
		t.Fatalf("the first once the second stopped: %+v", r)
	}
}

// A sandbox daemon runs beside the daemon that holds the machine, in a runtime directory of its
// own, and both daemons' status lists it while it runs. One in the holder's runtime directory, or in
// none of its own, is refused.
func TestRealASandboxDaemonRunsBesideTheDaemonAndIsListed(t *testing.T) {
	m := newMachine(t)
	main, sandbox := m.daemonAt("rt-main", false), m.daemonAt("rt-sandbox", true)
	if r := m.cliWith(main, "daemon", "ensure"); r.code != exitOK {
		t.Fatalf("the daemon: %+v", r)
	}
	if r := m.cliWith(sandbox, "daemon", "ensure"); r.code != exitOK {
		t.Fatalf("the sandbox daemon: %+v", r)
	}
	listed := fmt.Sprintf("sandbox\tpid %d\truntime %s\t", m.pidOf(sandbox), m.runtimeOf("rt-sandbox"))
	if r := m.cliWith(main, "daemon", "status"); !strings.Contains(r.stdout, listed) || strings.Contains(r.stdout, "role\tsandbox") {
		t.Fatalf("the daemon's status does not list the sandbox:\n%s", r.stdout)
	}
	if r := m.cliWith(sandbox, "daemon", "status"); !strings.Contains(r.stdout, "role\tsandbox") || !strings.Contains(r.stdout, listed) {
		t.Fatalf("the sandbox daemon's status:\n%s", r.stdout)
	}

	inHolders := append(append([]string{}, main...), production.EnvSandbox+"=1")
	if r := m.cliWith(inHolders, "daemon", "status"); r.code == exitOK || !strings.Contains(r.stderr, "runtime directory") {
		t.Fatalf("a sandbox in the holder's runtime directory: %+v; want it refused", r)
	}
	if r := m.cliWith([]string{"XDG_RUNTIME_DIR=", production.EnvSandbox + "=1"}, "daemon", "status"); r.code == exitOK || !strings.Contains(r.stderr, "XDG_RUNTIME_DIR") {
		t.Fatalf("a sandbox with no runtime directory of its own: %+v; want it refused", r)
	}

	if r := m.cliWith(sandbox, "daemon", "stop"); r.code != exitOK {
		t.Fatalf("stopping the sandbox: %+v", r)
	}
	if r := m.cliWith(main, "daemon", "status"); strings.Contains(r.stdout, "sandbox\t") {
		t.Fatalf("a sandbox that stopped is still listed:\n%s", r.stdout)
	}
}

// A daemon that fails after it was given its readiness pipe has written why to its log by the time
// the pipe closes without a byte, since the client that started it reads the log then.
func TestRealADaemonThatFailsHasSaidWhyWhenItsPipeCloses(t *testing.T) {
	m := newMachine(t)
	env := m.daemonAt("rt-refused", false)
	machineDir, err := runtimedir.OpenMachine(filepath.Join(m.root, "machine"), os.Getuid(), anyFS{})
	if err != nil {
		t.Fatal(err)
	}
	holder := &cliv1.MachineDaemon{Pid: uint32(os.Getpid()), RuntimeDir: "/run/holder/vibesys-cli", Version: "held"}
	claim, err := machineDir.Claim(t.Context(), clock.Immediate{}, holder)
	if err != nil {
		t.Fatal(err)
	}
	defer claim.Release()

	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	logPath := filepath.Join(m.root, "daemon.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		t.Fatal(err)
	}
	defer logFile.Close()
	cmd := exec.Command(self, "daemon", "run")
	cmd.Dir = m.repo
	cmd.Env = append(append(os.Environ(), roleVar+"="+roleCLI, "HOME="+filepath.Join(m.root, "home")), env...)
	cmd.Stdout, cmd.Stderr, cmd.ExtraFiles = logFile, logFile, []*os.File{w}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	w.Close()
	if n, _ := io.ReadFull(r, make([]byte, 1)); n != 0 {
		t.Fatal("the refused daemon said it served")
	}
	said, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(said), "holds this machine") || !strings.Contains(string(said), "/run/holder/vibesys-cli") {
		t.Fatalf("when the pipe closed, the log said %q; want the refusal naming the holder", said)
	}
	_ = cmd.Wait()
}
