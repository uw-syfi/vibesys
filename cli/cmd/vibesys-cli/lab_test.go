package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// newLabCLI is a command line over a daemon whose task has a Dockerfile and whose jobs keep
// artifacts, as a lab needs.
func newLabCLI(t *testing.T) *cli {
	t.Helper()
	c := newCLI(t)
	tree := tasktest.Scripted("kv", []string{"exit", "0"}, []string{"emit", `{"ops": 7.5}`})
	tree[task.TasksDir+"/kv/Dockerfile"] = task.File{Data: []byte("FROM scratch\n")}
	c.world.Tasks.Put("/repo", full, tree)
	c.world.Artifacts = func(_, taskName string, job core.JobID) string { return "/artifacts/" + taskName + "/" + string(job) }
	return c
}

// The lab commands, end to end through the daemon and the lab's supervisor: start waits until the
// lab is ready, exec streams a command's output and exits with its code, a detached command is
// listed and killed by its handle, files go in and come back out of the artifacts directory, and
// end returns once the lab has ended, which jobs lists as a lab.
func TestTheLabCommandsDriveALab(t *testing.T) {
	c := newLabCLI(t)
	r := c.run(withTask("lab", "start", "HEAD", "--for", "45m", "--wait", "--note", "explore")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "lab for 45m0s") || !strings.Contains(r.stdout, "\tready\t") || !strings.Contains(r.stdout, "rooms: candidate") {
		t.Fatalf("lab start = %+v", r)
	}
	id := strings.Fields(r.stdout)[0]
	if r := c.run(withTask("lab", "exec", id, "--env", "A=1", "--", "print", "hello", ";", "exit", "7")...); r.code != 7 || r.stdout != "hello\n" {
		t.Fatalf("lab exec = %+v", r)
	}
	if r := c.run(withTask("lab", "exec", id, "--detach", "--", "print", "up", ";", "hold", "forever")...); r.code != exitOK || !strings.HasPrefix(r.stdout, "p2\tcandidate\trunning") {
		t.Fatalf("lab exec --detach = %+v", r)
	}
	if r := c.run(withTask("lab", "kill", id, "p2", "--signal", "kill")...); r.code != exitOK {
		t.Fatalf("lab kill = %+v", r)
	}
	if r := c.run(withTask("lab", "ps", id)...); r.code != exitOK || !strings.Contains(r.stdout, "p1\tcandidate\texited 7") {
		t.Fatalf("lab ps = %+v", r)
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "in.txt"), []byte("data"), 0o600); err != nil {
		t.Fatal(err)
	}
	if r := c.run(withTask("lab", "cp", filepath.Join(dir, "in.txt"), id+":/vibesys/artifacts/sub/in.txt")...); r.code != exitOK || r.stdout != "copied 1 files, 4 bytes\n" {
		t.Fatalf("lab cp in = %+v", r)
	}
	out := filepath.Join(dir, "out")
	if r := c.run(withTask("lab", "cp", id+":sub", out)...); r.code != exitOK {
		t.Fatalf("lab cp out = %+v", r)
	}
	if data, err := os.ReadFile(filepath.Join(out, "in.txt")); err != nil || string(data) != "data" {
		t.Fatalf("copied back %q, %v", data, err)
	}
	if r := c.run(withTask("lab", "cp", id+":/etc/passwd", out)...); r.code != exitInvalidInput {
		t.Fatalf("lab cp of a path outside the artifacts = %+v", r)
	}
	if r := c.run(withTask("lab", "end", id)...); r.code != exitOK || !strings.Contains(r.stdout, id+"\tsucceeded (exit 0)") {
		t.Fatalf("lab end = %+v", r)
	}
	if r := c.run(withTask("jobs", "--kind", "lab")...); r.code != exitOK || !strings.Contains(r.stdout, "lab for 45m0s") || !strings.Contains(r.stdout, "note: explore") {
		t.Fatalf("jobs --kind lab = %+v", r)
	}
	if r := c.run(withTask("lab", "exec", id, "--", "print", "late")...); r.code != exitFailed {
		t.Fatalf("lab exec on an ended lab = %+v", r)
	}
}

// What the command line refuses before it calls the daemon.
func TestLabArgumentsAreCheckedBeforeTheDaemon(t *testing.T) {
	c := newLabCLI(t)
	for _, args := range [][]string{
		{"lab", "exec", "j1"},
		{"lab", "exec", "j1", "--in", "server", "--", "x"},
		{"lab", "kill", "j1", "p1", "--signal", "hup"},
		{"lab", "cp", "a", "b"},
		{"lab", "cp", "j1:a", "j2:b"},
		{"lab", "cp", "j1:../x", "b"},
		{"lab", "nope"},
	} {
		if r := c.run(withTask(args...)...); r.code != exitInvalidInput {
			t.Errorf("%v = %+v, want exit 2", args, r)
		}
	}
}

// labPath is total: it returns a clean path inside the artifacts directory, or refuses.
func TestALabPathStaysInTheArtifactsDirectory(t *testing.T) {
	for in, want := range map[string]string{
		"": "", ".": "", "a/b": "a/b", "a/./b/": "a/b", "/vibesys/artifacts": "", "/vibesys/artifacts/x/y": "x/y",
	} {
		if got, err := labPath(in); err != nil || got != want {
			t.Errorf("labPath(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, in := range []string{"..", "../a", "a/../../b", "/vibesys/artifactsx", "/etc"} {
		if got, err := labPath(in); err == nil {
			t.Errorf("labPath(%q) = %q, want refused", in, got)
		}
	}
}

// A lab started while the daemon drains stays queued: `queue` lists it as a lab waiting on the
// drain, and `lab start --wait` exits 1 at once saying so rather than waiting for an operator. Once
// the daemon resumes, the lab starts.
func TestALabStartedWhileTheDaemonDrainsIsQueued(t *testing.T) {
	c := newLabCLI(t)
	if r := c.run(withTask("jobs")...); r.code != exitOK {
		t.Fatalf("jobs, which starts the daemon: %+v", r)
	}
	if r := c.run("daemon", "drain", "--reason", "rollout"); r.code != exitOK {
		t.Fatalf("daemon drain: %+v", r)
	}
	r := c.run(withTask("lab", "start", "HEAD", "--for", "45m")...)
	if r.code != exitOK {
		t.Fatalf("lab start = %+v", r)
	}
	id := strings.Fields(r.stdout)[0]
	if q := c.run(withTask("queue")...); q.code != exitOK || !strings.Contains(q.stdout, id+"\tqueued\tlab for 45m0s\t") ||
		!strings.Contains(q.stdout, "waiting: the daemon is draining") {
		t.Fatalf("queue = %+v", q)
	}
	if r := c.run(withTask("lab", "ps", id, "--wait")...); r.code != exitFailed || !strings.Contains(r.stderr, "the daemon is draining") {
		t.Fatalf("lab ps --wait while the daemon drains = %+v", r)
	}
	if r := c.run("daemon", "resume"); r.code != exitOK {
		t.Fatalf("daemon resume: %+v", r)
	}
	if r := c.run(withTask("lab", "ps", id, "--wait")...); r.code != exitOK || !strings.Contains(r.stdout, "\tready\t") {
		t.Fatalf("lab ps --wait after the resume = %+v", r)
	}
	if r := c.run(withTask("lab", "end", id)...); r.code != exitOK {
		t.Fatalf("lab end = %+v", r)
	}
}
