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
	env    []string
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
	sidecar, err := filepath.Abs("../../orchestrator/sidecar")
	if err != nil {
		t.Fatal(err)
	}
	script, err := filepath.Abs("testdata/scripted_sidecar.py")
	if err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("uv", "sync", "--frozen", "--quiet", "--project", sidecar).CombinedOutput(); err != nil {
		t.Fatalf("prepare the sidecar environment: %v\n%s", err, out)
	}
	r := &rig{t: t, binary: binary, root: filepath.Join(dir, "r"), state: filepath.Join(dir, "state")}
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "XDG_RUNTIME_DIR=") && !strings.HasPrefix(kv, "VIBESYS_ORCHESTRATOR_") {
			r.env = append(r.env, kv)
		}
	}
	r.env = append(r.env,
		"VIBESYS_ORCHESTRATOR_STATE_DIR="+r.state,
		"VIBESYS_ORCHESTRATOR_SIDECAR=uv run --frozen --quiet --project "+sidecar+" python "+script,
	)
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

func (r *rig) serveLog() string {
	data, _ := os.ReadFile(r.state + ".serve.log")
	return string(data)
}

// watchUntil runs `run watch` and returns once a line satisfies want, failing after the bound.
func (r *rig) watchUntil(what string, want func(line string) bool) {
	r.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), bound)
	defer cancel()
	cmd := exec.CommandContext(ctx, r.binary, "--root", r.root, "run", "watch")
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
	// The sidecars are detached, so the test stops them itself.
	for _, pid := range r.sidecarPids() {
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
	var receipt struct{ RunID, AgentID string }
	var raw map[string]any
	if err := json.Unmarshal([]byte(r.client("run", "start", "--repository", repository, "--task", "demo", "--brief", "say hello")), &raw); err != nil {
		t.Fatal(err)
	}
	receipt.RunID, _ = raw["runId"].(string)
	receipt.AgentID, _ = raw["agentId"].(string)
	if receipt.RunID == "" || receipt.AgentID == "" {
		t.Fatalf("run start printed no run and agent: %v", raw)
	}
	r.watchUntil("the lead's first reply", func(line string) bool { return strings.Contains(line, "reply 1") })

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
	r.watchUntil("the run still running after the restart", func(line string) bool {
		return strings.Contains(line, `"snapshot"`) && strings.Contains(line, "RUN_STATUS_RUNNING")
	})
	r.client("send", receipt.AgentID, "say it again")
	r.watchUntil("the lead's second reply, from the same sidecar", func(line string) bool { return strings.Contains(line, "reply 2") })
	if got := r.sidecarPids(); len(got) != 1 || got[0] != pids[0] {
		t.Fatalf("the restart replaced the sidecar: before %v, after %v", pids, got)
	}

	r.client("run", "stop", receipt.RunID)
	r.watchUntil("the run ended", func(line string) bool {
		return strings.Contains(line, receipt.RunID) && strings.Contains(line, "RUN_STATUS_ENDED")
	})
}
