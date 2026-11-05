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

[profile]
command = ['sh', '-c', 'wget -q -O- "$VIBESYS_SERVICE_URL/health" && echo loaded']
timeout_seconds = 60
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
	// A profile starts the server under the wrapper and loads it with [profile]; it is never reused.
	for range 2 {
		r = m.cli("submit", "profile", "HEAD", "--task", "serve", "--wrap", "env PROFILED=1", "--wait")
		if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") || strings.Contains(r.stdout, "reused") {
			t.Fatalf("submit profile = %+v\n%s", r, m.logsOf(r))
		}
		if logs := m.logsOf(r); !strings.Contains(logs, "loaded") {
			t.Fatalf("the profile's output %q", logs)
		}
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
