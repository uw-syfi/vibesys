//go:build e2e

package e2e

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// desktopUIEnv opts the desktop path test in to the UI observer: the built desktop app is
// launched against the world the test started, and an operator presses Stop in it. CI sets it in
// the desktop-path job, which has the node workspace, the Electron runtime and a virtual display
// the observer needs; without it the test runs as a headless conductor and presses Stop itself.
const desktopUIEnv = "VIBESYS_E2E_DESKTOP_UI"

// uiBound guards the observer against a hang. It covers building the app (a cold Go cache builds
// the gateway for this machine's target) and the run of the Playwright spec; nothing waits for it to pass.
const uiBound = 6 * time.Minute

func init() {
	switch os.Getenv(desktopUIEnv) {
	case "":
	case "1":
		observeUI = observeWithDesktopApp
	default:
		panic(desktopUIEnv + " must be 1 or unset, got " + os.Getenv(desktopUIEnv))
	}
}

// uiSpecWorld is what the Playwright spec (clients/apps/desktop/e2e/observe-run.ts) reads from
// VIBESYS_E2E_WORLD to attach to the world and to know what the screens must show. The names are
// the daemon's own, so the spec compares the screens with the daemon's report of the run and
// with no value of its own.
type uiSpecWorld struct {
	// Home is the HOME the app runs with: its .vibesys is the running daemon's root.
	Home   string    `json:"home"`
	RunID  string    `json:"runId"`
	Task   string    `json:"task"`
	Report string    `json:"report"`
	Agents []uiAgent `json:"agents"`
}

// observeWithDesktopApp runs the Playwright spec of the desktop app against the world. The spec
// asserts the screens, presses Stop in the UI and closes the app; this then asserts that nothing
// of the app outlives it.
func observeWithDesktopApp(t *testing.T, world uiWorld) {
	t.Helper()
	clients, err := filepath.Abs("../../clients")
	if err != nil {
		t.Fatal(err)
	}
	mark := newMark(t)
	spec, err := json.Marshal(uiSpecWorld{Home: world.Home, RunID: world.RunID, Task: world.Task, Report: world.Report, Agents: world.Agents})
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), uiBound)
	defer cancel()
	// test:e2e builds the app, then runs the spec; the build is a no-op for what is already built.
	cmd := exec.CommandContext(ctx, "pnpm", "--filter", "@vibesys/desktop", "test:e2e")
	cmd.Dir = clients
	// The app attaches to the running daemon, but needs the gateway resources of this machine to
	// start; it does not need the other targets a packaged build ships.
	cmd.Env = append(os.Environ(), "VIBESYS_E2E_WORLD="+string(spec), "VIBESYS_E2E_MARK="+mark,
		"VIBESYS_GATEWAY_TARGETS="+runtime.GOOS+"-"+runtime.GOARCH)
	// The app's processes are the spec's children, so a hang is cut off at the whole group.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) }
	out, runErr := cmd.CombinedOutput()

	// Whatever the spec did, nothing it started may be left: not the app and its helpers, and not a
	// gateway the app started for itself (it dials the running one, so it starts none).
	leaked := untilNoneRunning(func() []int { return processesMarked(mark) })
	for _, pid := range leaked {
		t.Errorf("process %d started by the desktop app still runs after the observer; killing it", pid)
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
	gatewayDir := filepath.Join(world.Home, ".vibesys", "gateway")
	pids, err := leakedHostsUnder(gatewayDir, []string{"vibesysd", "vibesys-host"}, processTable())
	if err != nil {
		t.Errorf("check for leaked gateways: %v", err)
	}
	for _, pid := range pids {
		t.Errorf("the desktop app's own gateway (%d) runs; it should have used the running one", pid)
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}

	if runErr != nil {
		t.Fatalf("the desktop app observer failed: %v\n%s", runErr, out)
	}
	t.Logf("the desktop app observer passed:\n%s", tail(string(out), 40))
}

// newMark is a value no other process carries, set in the environment of everything the observer
// starts so that what it leaves behind can be found.
func newMark(t *testing.T) string {
	t.Helper()
	b := make([]byte, 12)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(b)
}

// processesMarked lists the live processes whose environment carries the mark, which every
// descendant of the observer inherits.
func processesMarked(mark string) []int {
	entries, _ := filepath.Glob("/proc/[0-9]*/environ")
	want := []byte("VIBESYS_E2E_MARK=" + mark + "\x00")
	var pids []int
	for _, entry := range entries {
		data, err := os.ReadFile(entry)
		if err != nil || !bytes.Contains(data, want) {
			continue
		}
		pid, err := strconv.Atoi(strings.Split(entry, "/")[2])
		if err == nil && running(pid) {
			pids = append(pids, pid)
		}
	}
	return pids
}

// tail is the last n lines of text.
func tail(text string, n int) string {
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}
