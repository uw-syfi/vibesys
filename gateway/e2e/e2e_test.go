//go:build e2e

// Package e2e runs vibesysd end to end with real processes: the gateway serving the orchestrator,
// real sidecar processes over agentshim's scripted Claude peer, and the headless client. No model
// is called. Run it from gateway/ with `go test -tags e2e ./e2e -v`; it needs uv and Python.
package e2e

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// bound guards each wait against a hang. Nothing waits for it to pass.
const bound = 90 * time.Second

type rig struct {
	t      *testing.T
	binary string
	root   string
	state  string
	dir    string
	env    []string
	// marketplace is the plugin marketplace the orchestrator was given, once the test sets it.
	marketplace string
}

func newRig(t *testing.T) *rig {
	t.Helper()
	// Socket paths must fit in 107 bytes, so the state lives in a short directory of its own.
	dir, err := os.MkdirTemp("", "vsd-e2e-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if t.Failed() {
			t.Logf("state kept in %s", dir)
			return
		}
		os.RemoveAll(dir)
	})
	binary := filepath.Join(dir, "vibesysd")
	build := exec.Command("go", "build", "-o", binary, "../cmd/vibesysd")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build vibesysd: %v\n%s", err, out)
	}
	cli := filepath.Join(dir, "vibesys-cli")
	buildCLI := exec.Command("go", "build", "-o", cli, "./cmd/vibesys-cli")
	buildCLI.Dir = "../../cli"
	if out, err := buildCLI.CombinedOutput(); err != nil {
		t.Fatalf("build vibesys-cli: %v\n%s", err, out)
	}
	sidecar, err := filepath.Abs("../../orchestrator/sidecar")
	if err != nil {
		t.Fatal(err)
	}
	script, err := filepath.Abs("../../orchestrator/sidecar/e2e/scripted_sidecar.py")
	if err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("uv", "sync", "--frozen", "--quiet", "--project", sidecar).CombinedOutput(); err != nil {
		t.Fatalf("prepare the sidecar environment: %v\n%s", err, out)
	}
	// The daemon's root is the .vibesys directory of a home of its own (dir). A client that finds
	// the gateway by the user's home, as the desktop app does, reaches this one when its HOME is dir.
	r := &rig{t: t, binary: binary, dir: dir, root: filepath.Join(dir, ".vibesys"), state: filepath.Join(dir, "state")}
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "XDG_RUNTIME_DIR=") && !strings.HasPrefix(kv, "XDG_STATE_HOME=") && !strings.HasPrefix(kv, "VIBESYS_") {
			r.env = append(r.env, kv)
		}
	}
	// The runtime directory holds vibesys-cli's socket and the agent socket (whose name carries a
	// hash of the state directory, so it never meets another orchestrator's). Both live under dir,
	// so the test touches nothing of the user running it.
	r.env = append(r.env,
		"XDG_RUNTIME_DIR="+filepath.Join(dir, "rt"),
		"XDG_STATE_HOME="+filepath.Join(dir, "xdg-state"),
		// The scripted sidecar imports its sibling modules as the package e2e of the sidecar project.
		"PYTHONPATH="+sidecar,
		"VIBESYS_ORCHESTRATOR_STATE_DIR="+r.state,
		"VIBESYS_ORCHESTRATOR_CLI="+cli,
		"VIBESYS_ORCHESTRATOR_SIDECAR=uv run --frozen --quiet --project "+sidecar+" python "+script,
	)
	for _, d := range []string{"rt", "xdg-state"} {
		if err := os.MkdirAll(filepath.Join(dir, d), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	r.useMarketplace()
	t.Cleanup(r.killSidecars)
	return r
}

// serve starts a gateway this test owns, so it can kill it.
func (r *rig) serve() *exec.Cmd {
	r.t.Helper()
	cmd := exec.Command(r.binary, "serve", "--root", r.root, "--idle-timeout", "10m")
	cmd.Env = r.env
	log, err := os.OpenFile(filepath.Join(r.state+".serve.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		r.t.Fatal(err)
	}
	cmd.Stdout, cmd.Stderr = log, log
	if err := cmd.Start(); err != nil {
		r.t.Fatal(err)
	}
	r.t.Cleanup(func() {
		if r.t.Failed() && os.Getenv("E2E_KEEP") != "" {
			return
		}
		if r.t.Failed() {
			_ = cmd.Process.Signal(syscall.SIGQUIT) // a goroutine dump, into the serve log
			_ = cmd.Wait()
		}
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		log.Close()
	})
	return cmd
}

// client runs one client verb and returns its stdout.
func (r *rig) client(args ...string) string {
	r.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), bound)
	defer cancel()
	cmd := exec.CommandContext(ctx, r.binary, append([]string{"--root", r.root}, args...)...)
	cmd.Env = r.env
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		r.t.Fatalf("vibesysd %s: %v\n%s\nserve log:\n%s", strings.Join(args, " "), err, stderr.String(), r.serveLog())
	}
	return string(out)
}

// receipt is what a client verb that dispatches a command prints.
type receipt struct {
	RunID, AgentID string
	// Sequence is the receipt's own event. What the command causes later, such as an agent's
	// reply, comes after it.
	Sequence uint64
}

// receipt runs a client verb that dispatches a command and returns its receipt.
func (r *rig) receipt(args ...string) receipt {
	r.t.Helper()
	var raw struct {
		RunID    string `json:"runId"`
		AgentID  string `json:"agentId"`
		Sequence string `json:"sequence"`
	}
	if err := json.Unmarshal([]byte(r.client(args...)), &raw); err != nil {
		r.t.Fatal(err)
	}
	sequence, err := strconv.ParseUint(raw.Sequence, 10, 64)
	if err != nil {
		r.t.Fatalf("vibesysd %s printed no receipt sequence: %+v", strings.Join(args, " "), raw)
	}
	return receipt{RunID: raw.RunID, AgentID: raw.AgentID, Sequence: sequence}
}

func (r *rig) serveLog() string {
	data, _ := os.ReadFile(r.state + ".serve.log")
	return string(data)
}

// watchUntil runs `run watch` and returns once a line satisfies want, failing after the bound.
//
// With after zero the watch starts with a snapshot of runs and agents, so want may only look for
// what a snapshot holds. An item is in no snapshot: a watch for one must replay the log after a
// sequence from before the item, such as the receipt of the command that caused it, or it misses
// an item committed before the watch opened.
func (r *rig) watchUntil(what string, after uint64, want func(line string) bool) {
	r.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), bound)
	defer cancel()
	cmd := exec.CommandContext(ctx, r.binary, "--root", r.root, "run", "watch", "--after", strconv.FormatUint(after, 10))
	cmd.Env = r.env
	out, err := cmd.StdoutPipe()
	if err != nil {
		r.t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		r.t.Fatal(err)
	}
	defer func() { cancel(); _ = cmd.Wait() }()
	scanner := bufio.NewScanner(out)
	scanner.Buffer(make([]byte, 1<<20), 16<<20)
	var seen []string
	for scanner.Scan() {
		line := scanner.Text()
		seen = append(seen, line)
		if want(line) {
			return
		}
		if strings.Contains(line, "RUN_STATUS_FAILED") {
			break
		}
	}
	r.t.Fatalf("watch ended without %s within %s; it printed:\n%s\nserve log:\n%s", what, bound, strings.Join(seen, "\n"), r.serveLog())
}

// sidecarPids lists the sidecar processes the launcher recorded.
func (r *rig) sidecarPids() []int {
	files, _ := filepath.Glob(filepath.Join(r.state, "sessions", "*", "*", "sidecar.pid"))
	var pids []int
	for _, f := range files {
		data, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		if pid, err := strconv.Atoi(strings.TrimSpace(string(data))); err == nil {
			pids = append(pids, pid)
		}
	}
	return pids
}

func (r *rig) killSidecars() {
	if r.t.Failed() && os.Getenv("E2E_KEEP") != "" {
		return
	}
	// The sidecars are detached, so the test stops them itself. Nothing of the world may outlive
	// the test: no sidecar, and no process of the gateway binary (the daemon, which its own cleanup
	// killed, and the tool servers the agents ran).
	pids := r.sidecarPids()
	for _, pid := range pids {
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
	for _, pid := range untilNoneRunning(func() []int {
		left := processesOf(r.binary)
		for _, pid := range pids {
			if running(pid) {
				left = append(left, pid)
			}
		}
		return left
	}) {
		r.t.Errorf("process %d of the world still runs after the test stopped it", pid)
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
}

func alive(pid int) bool { return syscall.Kill(pid, 0) == nil }

func TestARunSurvivesADaemonRestart(t *testing.T) {
	r := newRig(t)
	repository := t.TempDir()
	if out, err := exec.Command("git", "init", "-q", repository).CombinedOutput(); err != nil {
		t.Fatalf("git init: %v\n%s", err, out)
	}

	first := r.serve()
	started := r.receipt("run", "start", "--repository", repository, "--task", "demo", "--brief", "say hello")
	if started.RunID == "" || started.AgentID == "" {
		t.Fatalf("run start printed no run and agent: %+v", started)
	}
	r.watchUntil("the lead's first reply", started.Sequence, func(line string) bool { return strings.Contains(line, "reply 1") })

	// The daemon dies mid-run. The sidecar, which holds the agent, does not.
	pids := r.sidecarPids()
	if len(pids) != 1 {
		t.Fatalf("want one sidecar for the lead, found pids %v", pids)
	}
	if err := first.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = first.Wait()
	if !alive(pids[0]) {
		t.Fatalf("the sidecar %d died with the daemon", pids[0])
	}

	r.serve()
	r.watchUntil("the run still running after the restart", 0, func(line string) bool {
		return strings.Contains(line, `"snapshot"`) && strings.Contains(line, "RUN_STATUS_RUNNING")
	})
	sent := r.receipt("send", started.AgentID, "say it again")
	r.watchUntil("the lead's second reply, from the same sidecar", sent.Sequence, func(line string) bool { return strings.Contains(line, "reply 2") })
	if got := r.sidecarPids(); len(got) != 1 || got[0] != pids[0] {
		t.Fatalf("the restart replaced the sidecar: before %v, after %v", pids, got)
	}

	r.client("run", "stop", started.RunID)
	// A run's status is in the snapshot, so this watch needs no replay.
	r.watchUntil("the run ended", 0, func(line string) bool {
		return strings.Contains(line, started.RunID) && strings.Contains(line, "RUN_STATUS_ENDED")
	})
}
