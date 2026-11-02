//go:build docker && unix

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// These tests run the tool as shipped, as real_test.go does, on a task that has a Dockerfile, so
// the jobs run in the task's image in real containers. They need a reachable Docker daemon, and
// the image's base, which Docker pulls once. The image built from the Dockerfile stays, as the
// cache of the next run of the tests.

// pinned is a base image, by tag and digest. A Dockerfile that is the same text builds the same
// image tag, so repeated runs of the tests share one image.
const pinned = "busybox:1.36.1@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"

// commitDockerTask commits a repository whose task "bench" ships a Dockerfile. Its check says who
// runs it, and its benchmark prints a line for every sample it takes.
func (m *machine) commitDockerTask() {
	m.t.Helper()
	m.git("init", "-q", "-b", "main")
	m.write("src.txt", "committed\n", 0o644)
	dir := task.TasksDir + "/bench/"
	m.write(dir+task.ManifestName, `
version = 1
backends = ["cpu"]

[accuracy]
command = ['sh', '-c', 'test "$(cat ${PROJECT_ROOT}/src.txt)" = committed && echo "user $(id -u)"']
timeout_seconds = 60

[benchmark]
command = ['sh', '-c', 'echo sampled; printf "{\"ops\": 12.5}" > "$2"', 'sh']
timeout_seconds = 60

[benchmark.result]
json_argument = "--out"
metric = "ops"
`, 0o644)
	m.write(dir+task.ObjectiveName, "[[objective]]\nname = \"ops\"\ndirection = \"max\"\n", 0o644)
	m.write(dir+"Dockerfile", "FROM "+pinned+"\nRUN true\n", 0o644)
	m.git("add", "-A")
	m.git("commit", "-q", "-m", "task")
}

// jobOf is the id a submission printed.
func jobOf(t *testing.T, r outcome) string {
	t.Helper()
	fields := strings.Fields(r.stdout)
	if len(fields) == 0 {
		t.Fatalf("no job in %+v", r)
	}
	return fields[0]
}

// scopeContainers lists the containers the tool made for this machine's daemon, running or not.
func (m *machine) scopeContainers() []string {
	m.t.Helper()
	rt := runtimedir.Resolve(func(k string) string {
		if k == "XDG_RUNTIME_DIR" {
			return filepath.Join(m.root, "rt")
		}
		return ""
	}, os.Getuid())
	out, err := exec.Command("docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "label="+docker.LabelScope+"="+docker.ScopeFor(rt)).Output()
	if err != nil {
		m.t.Fatalf("docker ps: %v", err)
	}
	return strings.Fields(string(out))
}

func TestRealDockerTaskRunsInItsImageAsTheUserAndTopsUpFromTheCache(t *testing.T) {
	m := newMachine(t)
	m.commitDockerTask()

	// The task has a Dockerfile, so its jobs run in docker without being told to.
	r := m.cli("submit", "check", "HEAD", "--wait")
	if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") {
		t.Fatalf("submit check = %+v", r)
	}
	if logs := m.cli("logs", jobOf(t, r)); !strings.Contains(logs.stdout, "user "+strconv.Itoa(os.Getuid())) {
		t.Errorf("the check's output %q does not show it ran as the invoking user", logs.stdout)
	}

	sampled := func(r outcome) int {
		t.Helper()
		if r.code != exitOK || !strings.Contains(r.stdout, "succeeded") {
			t.Fatalf("submit = %+v", r)
		}
		return strings.Count(m.cli("logs", jobOf(t, r)).stdout, "sampled\n")
	}
	if n := sampled(m.cli("submit", "measure", "HEAD", "--samples", "3", "--wait")); n != 3 {
		t.Fatalf("the first measure took %d samples, want 3", n)
	}
	// The image is the same, so the saved samples count and only the two missing are taken.
	if n := sampled(m.cli("submit", "measure", "HEAD", "--samples", "5", "--wait")); n != 2 {
		t.Fatalf("the second measure took %d samples, want the 2 the cache lacks", n)
	}
	r = m.cli("submit", "measure", "HEAD", "--samples", "3")
	if r.code != exitOK || !strings.Contains(r.stdout, "(reused a saved result)") {
		t.Fatalf("a measure the cache holds = %+v", r)
	}

	if left := m.scopeContainers(); len(left) != 0 {
		t.Errorf("containers %v left after the jobs", left)
	}
}

func TestRealDockerTaskIsRefusedClearlyWhenDockerIsNotThere(t *testing.T) {
	m := newMachine(t)
	m.commitDockerTask()
	// A machine with git and without a docker program: the submission says so, and nothing runs on
	// the host in its place.
	git, err := exec.LookPath("git")
	if err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(m.root, "bin")
	if err := os.MkdirAll(bin, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(git, filepath.Join(bin, "git")); err != nil {
		t.Fatal(err)
	}
	r := m.cliWith([]string{"PATH=" + bin}, "submit", "check", "HEAD", "--wait")
	if r.code == exitOK || !strings.Contains(r.stdout+r.stderr, "docker") {
		t.Fatalf("submit check without docker = %+v, want a failure that names docker", r)
	}
}
