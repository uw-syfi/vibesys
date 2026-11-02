package main

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

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
		os.RemoveAll(root)
	})
	return m
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
version = 1
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
	cmd := exec.Command(self, args...)
	cmd.Dir = m.repo
	// The runtime files and the index are under the test's own directories, so the tool touches
	// nothing of the user running the test.
	cmd.Env = append(os.Environ(), roleVar+"="+roleCLI,
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
