package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/production"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// The tests here run the tool as shipped: the test binary stands in for vibesys-cli (see roleCLI),
// starts its own daemon and supervisors, and the jobs run real programs on this machine, on a real
// git repository. Nothing is a fake. Every wait is the tool's own `--wait`, which returns when the
// job is final.

// machine is a user's home for one test: a git repository with a task, and the places the tool
// keeps its runtime files and its index.
type machine struct {
	t       *testing.T
	root    string
	repo    string
	counter string
}

func newMachine(t *testing.T) *machine {
	t.Helper()
	root, err := os.MkdirTemp("", "vsr")
	if err != nil {
		t.Fatal(err)
	}
	root, _ = filepath.EvalSymlinks(root)
	m := &machine{t: t, root: root, repo: filepath.Join(root, "repo"), counter: filepath.Join(root, "samples")}
	for _, d := range []string{m.repo, filepath.Join(root, "rt"), filepath.Join(root, "home")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		m.cli("daemon", "stop")
		// Whatever the stop did not end, a wedged daemon or a supervisor of a job it left running,
		// ends here, so that no daemon outlives the test.
		m.sweep()
		os.RemoveAll(root)
	})
	return m
}

// sweep kills every process of this machine's root, the daemons, supervisors and clients of every
// runtime directory under it, and returns once they are gone. A process is of the root when its
// XDG_RUNTIME_DIR is under it, which the processes the tool starts inherit.
func (m *machine) sweep() {
	m.t.Helper()
	pids := processesUnder(m.root)
	for _, pid := range pids {
		_ = syscall.Kill(pid, syscall.SIGKILL)
	}
	// Bounded only against a process the kernel will not end.
	ctx, cancel := context.WithTimeout(context.Background(), hangBound)
	defer cancel()
	for _, pid := range pids {
		for alive(pid) {
			if !clock.Sleep(ctx, clock.System{}, 10*time.Millisecond) {
				m.t.Errorf("process %d of %s is still there after SIGKILL", pid, m.root)
				return
			}
		}
	}
}

// alive says whether the process runs: one that ended and is waiting to be reaped does not.
func alive(pid int) bool {
	stat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return false
	}
	// The state follows the command name, which is in parentheses and may hold anything.
	i := bytes.LastIndexByte(stat, ')')
	return i < 0 || i+2 >= len(stat) || stat[i+2] != 'Z'
}

// processesUnder are the processes other than this one whose XDG_RUNTIME_DIR is root or under it.
func processesUnder(root string) []int {
	entries, _ := os.ReadDir("/proc")
	var out []int
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == os.Getpid() {
			continue
		}
		env, err := os.ReadFile("/proc/" + e.Name() + "/environ")
		if err != nil {
			continue
		}
		for _, kv := range bytes.Split(env, []byte{0}) {
			if v, ok := strings.CutPrefix(string(kv), "XDG_RUNTIME_DIR="); ok && (v == root || strings.HasPrefix(v, root+"/")) {
				out = append(out, pid)
				break
			}
		}
	}
	return out
}

func (m *machine) git(args ...string) string {
	m.t.Helper()
	cmd := exec.Command("git", append([]string{"-C", m.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"}, args...)...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		m.t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func (m *machine) write(rel, content string, mode os.FileMode) {
	m.t.Helper()
	full := filepath.Join(m.repo, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		m.t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), mode); err != nil {
		m.t.Fatal(err)
	}
}

// commitTask commits a repository whose task "bench" checks that the checkout holds the committed
// source, and whose benchmark adds a byte to a counter file for every sample it takes.
func (m *machine) commitTask() {
	m.t.Helper()
	m.git("init", "-q", "-b", "main")
	m.write("src.txt", "committed\n", 0o644)
	dir := task.TasksDir + "/bench/"
	m.write(dir+task.ManifestName, fmt.Sprintf(`
version = "0.1"
backends = ["cpu"]

[accuracy]
command = ['sh', '-c', 'test "$(cat ${PROJECT_ROOT}/src.txt)" = committed']
timeout_seconds = 60

[benchmark]
command = ['sh', '-c', 'printf x >> %s; printf "{\"ops\": 12.5}" > "$2"', 'sh']
timeout_seconds = 60

[benchmark.result]
json_argument = "--out"
metric = "ops"
`, m.counter), 0o644)
	m.write(dir+task.ObjectiveName, "[[objective]]\nname = \"ops\"\ndirection = \"max\"\n", 0o644)
	m.git("add", "-A")
	m.git("commit", "-q", "-m", "task")
	// What is committed is what runs: a later edit of the working tree changes nothing.
	m.write("src.txt", "edited after the commit\n", 0o644)
}

type outcome struct {
	code           int
	stdout, stderr string
}

// cli runs the tool in the repository, as a user's shell would.
func (m *machine) cli(args ...string) outcome { return m.cliWith(nil, args...) }

// cliWith runs the tool with the variables added to its environment.
func (m *machine) cliWith(env []string, args ...string) outcome {
	m.t.Helper()
	self, err := os.Executable()
	if err != nil {
		m.t.Fatal(err)
	}
	// A command that outlasts hangBound is a hang, and is killed, so no client outlives the test.
	ctx, cancel := context.WithTimeout(context.Background(), hangBound)
	defer cancel()
	cmd := exec.CommandContext(ctx, self, args...)
	cmd.Dir = m.repo
	// The runtime files and the index are under the test's own directories, so the tool touches
	// nothing of the user running the test, and its daemon is a sandbox daemon, which holds no
	// machine lock and which the user's daemon lists while it runs.
	cmd.Env = append(os.Environ(), roleVar+"="+roleCLI, production.EnvSandbox+"=1",
		"XDG_RUNTIME_DIR="+filepath.Join(m.root, "rt"), "HOME="+filepath.Join(m.root, "home"), "VIBESYS_TASK=bench")
	cmd.Env = append(cmd.Env, env...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	err = cmd.Run()
	var exit *exec.ExitError
	switch {
	case err == nil:
		return outcome{0, stdout.String(), stderr.String()}
	case errors.As(err, &exit):
		return outcome{exit.ExitCode(), stdout.String(), stderr.String()}
	default:
		m.t.Fatalf("running the tool: %v", err)
		return outcome{}
	}
}

// samples is how many times the benchmark has run.
func (m *machine) samples() int {
	m.t.Helper()
	data, err := os.ReadFile(m.counter)
	if errors.Is(err, os.ErrNotExist) {
		return 0
	}
	if err != nil {
		m.t.Fatal(err)
	}
	return len(data)
}

func TestRealMeasureOfARealRepositoryIsCachedAndTopsUp(t *testing.T) {
	t.Parallel()
	m := newMachine(t)
	m.commitTask()

	r := m.cli("submit", "check", "HEAD", "--wait")
	if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") {
		t.Fatalf("submit check = %+v", r)
	}

	// The first measure takes three samples.
	r = m.cli("submit", "measure", "HEAD", "--samples", "3", "--wait")
	if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") || m.samples() != 3 {
		t.Fatalf("submit measure --samples 3 = %+v, %d samples taken", r, m.samples())
	}

	// A larger one takes only the two the cache lacks.
	r = m.cli("submit", "measure", "HEAD", "--samples", "5", "--wait")
	if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") || m.samples() != 5 {
		t.Fatalf("submit measure --samples 5 = %+v, %d samples taken in all, want 5", r, m.samples())
	}

	// The sandbox daemon took the lease of its measure in its own runtime directory, and the
	// machine's lease directory, which the daemon of the user and of every other user share, is
	// not the one it took it in.
	if _, err := os.Stat(filepath.Join(m.root, "rt", "vibesys-cli", "leases", "machine.lock")); err != nil {
		t.Fatalf("the sandbox daemon's measure did not lease in its own runtime directory: %v", err)
	}

	// One the cache holds runs nothing.
	r = m.cli("submit", "measure", "HEAD", "--samples", "3")
	if r.code != exitOK || !strings.Contains(r.stdout, "(reused a saved result)") || m.samples() != 5 {
		t.Fatalf("a measure the cache holds = %+v, %d samples taken in all, want 5", r, m.samples())
	}

	// --no-cache takes them all again.
	r = m.cli("submit", "measure", "HEAD", "--samples", "2", "--no-cache", "--wait")
	if r.code != exitOK || m.samples() != 7 {
		t.Fatalf("a no-cache measure = %+v, %d samples taken in all, want 7", r, m.samples())
	}
}

func TestRealCheckThatFailsFailsTheJobAndSavesNothing(t *testing.T) {
	t.Parallel()
	m := newMachine(t)
	m.commitTask()
	// Break the check: the committed source is no longer what it expects.
	m.write("src.txt", "broken\n", 0o644)
	m.git("commit", "-q", "-am", "break")

	r := m.cli("submit", "check", "HEAD", "--wait")
	if r.code != exitFailed || !strings.Contains(r.stdout, "failed") {
		t.Fatalf("submit check = %+v", r)
	}
	r = m.cli("submit", "measure", "HEAD", "--samples", "2", "--wait")
	if r.code != exitFailed || m.samples() != 0 {
		t.Fatalf("a measure of a commit that fails its check = %+v, %d samples taken", r, m.samples())
	}
}
