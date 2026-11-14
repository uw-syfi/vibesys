//go:build docker && unix

package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// commitTwoImageTask commits a repository whose task "serve" is of task schema 0.2: the
// candidate's image runs busybox's web server over the commit's serve/ directory, and the task's
// image reaches it over the network the two containers share.
func (m *machine) commitTwoImageTask() {
	m.t.Helper()
	m.git("init", "-q", "-b", "main")
	m.write("serve/Dockerfile", "FROM "+pinned+"\nCOPY . /srv\n", 0o644)
	m.write("serve/health", "ok\n", 0o644)
	m.write("serve/.dockerignore", "scratch\n", 0o644)
	dir := task.TasksDir + "/serve/"
	m.write(dir+task.ManifestName, `
version = "0.2"
backends = ["cpu"]
metric = "ops"

[candidate]
dockerfile = "serve/Dockerfile"
context = "serve"
serve = ["httpd", "-f", "-v", "-p", "${PORT}", "-h", "/srv"]
ready = "/health"
startup_timeout_seconds = 60
allowed_bases = ["`+pinned+`"]

[accuracy]
command = ['sh', '-c', 'test "$(wget -q -O- "$VIBESYS_SERVICE_URL/health")" = ok && echo "served at $VIBESYS_SERVICE_URL"']
timeout_seconds = 60

[benchmark]
command = ['sh', '-c', 'wget -q -O- "$VIBESYS_SERVICE_URL/health" >/dev/null && echo sampled && printf "%s\n%s\n" "{\"kind\":\"hello\",\"protocol\":2,\"metrics\":{\"ops\":{}}}" "{\"kind\":\"result\",\"values\":{\"ops\":3.5}}" > "$2"', 'sh']
timeout_seconds = 60
result_protocol = 2

`, 0o644)
	m.write(dir+task.ObjectiveName, "[[objective]]\nname = \"ops\"\ndirection = \"max\"\n", 0o644)
	m.write(dir+"Dockerfile", "FROM "+pinned+"\nRUN true\n", 0o644)
	m.git("add", "-A")
	m.git("commit", "-q", "-m", "task")
}

func (m *machine) logsOf(r outcome) string {
	m.t.Helper()
	return m.cli("logs", "--task", "serve", jobOf(m.t, r)).stdout
}

func TestRealTwoImageTaskServesTheCandidateBesideTheTask(t *testing.T) {
	m := newMachine(t)
	m.commitTwoImageTask()

	r := m.cli("submit", "check", "HEAD", "--task", "serve", "--wait")
	if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") {
		t.Fatalf("submit check = %+v\n%s", r, m.logsOf(r))
	}
	if logs := m.logsOf(r); !strings.Contains(logs, "served at http://127.0.0.1:") || !strings.Contains(logs, "[candidate]") {
		t.Errorf("the check's output %q does not show it reached the candidate's server", logs)
	}
	r = m.cli("submit", "measure", "HEAD", "--task", "serve", "--samples", "2", "--wait")
	if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") {
		t.Fatalf("submit measure = %+v\n%s", r, m.logsOf(r))
	}
	if n := strings.Count(m.logsOf(r), "sampled\n"); n != 2 {
		t.Fatalf("took %d samples, want 2", n)
	}
	// A command in the task's image runs beside the candidate's server, which starts under the
	// wrapper, as a profile is taken; it is never reused.
	load := []string{"--", "sh", "-c", `wget -q -O- "$VIBESYS_SERVICE_URL/health" && echo loaded`}
	for range 2 {
		r = m.cli(append([]string{"submit", "command", "HEAD", "--task", "serve", "--image", "harness", "--wrap", "env PROFILED=1", "--wait"}, load...)...)
		if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") || strings.Contains(r.stdout, "reused") || !strings.Contains(r.stdout, "[harness]") {
			t.Fatalf("submit command --image harness = %+v\n%s", r, m.logsOf(r))
		}
		// Candidate request logs can interleave with the harness's stdout between these lines.
		if logs := m.logsOf(r); !strings.Contains(logs, "\nok\n") || !strings.Contains(logs, "\nloaded\n") || !strings.Contains(logs, "[command] image: harness") {
			t.Fatalf("the harness command's output %q", logs)
		}
	}
	// A command in the candidate's image runs there, over the commit's serve/ directory, with no server.
	r = m.cli("submit", "command", "HEAD", "--task", "serve", "--image", "candidate", "--wait", "--", "cat", "/srv/health")
	if r.code != exitOK || !strings.Contains(r.stdout, "[candidate]") {
		t.Fatalf("submit command --image candidate = %+v\n%s", r, m.logsOf(r))
	}
	if logs := m.logsOf(r); !strings.Contains(logs, "ok\n") || !strings.Contains(logs, "[command] image: candidate") {
		t.Fatalf("the candidate command's output %q", logs)
	}
	if left := m.scopeContainers(); len(left) != 0 {
		t.Errorf("containers %v left after the jobs", left)
	}
}

func TestRealTwoImageTaskWhoseServerIsNeverReadyFailsWithItsLog(t *testing.T) {
	m := newMachine(t)
	m.commitTwoImageTask()
	manifest := task.TasksDir + "/serve/" + task.ManifestName
	data, err := os.ReadFile(filepath.Join(m.repo, manifest))
	if err != nil {
		t.Fatal(err)
	}
	text := strings.Replace(string(data), `ready = "/health"`, `ready = "/missing"`, 1)
	text = strings.Replace(text, "startup_timeout_seconds = 60", "startup_timeout_seconds = 3", 1)
	m.write(manifest, text, 0o644)
	m.git("commit", "-q", "-am", "never ready")
	r := m.cli("submit", "check", "HEAD", "--task", "serve", "--wait")
	if r.code == exitOK || !strings.Contains(r.stdout+r.stderr+m.logsOf(r), "not ready") {
		t.Fatalf("submit check = %+v", r)
	}
	if left := m.scopeContainers(); len(left) != 0 {
		t.Errorf("containers %v left after the job", left)
	}
}
