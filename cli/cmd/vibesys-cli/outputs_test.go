package main

import (
	"encoding/json"
	"os"
	"path"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// newOutputsCLI is a command line whose daemon keeps each job's artifacts in its world's files.
func newOutputsCLI(t *testing.T) *cli {
	t.Helper()
	c := newCLI(t)
	c.world.Artifacts = func(repository, task string, job core.JobID) string {
		return path.Join("/state", task, string(job))
	}
	return c
}

// A command submitted with --outputs keeps the files it left; outputs lists them with the job's
// provenance, and --to copies them, byte for byte, into a directory it makes, replacing what is
// there. A failed command keeps its files too.
func TestOutputsListsAndCopiesTheFilesAJobKept(t *testing.T) {
	c := newOutputsCLI(t)
	r := c.run(withTask("submit", "command", "--image", "harness", "--env", "host", "--outputs", "--wait", "HEAD", "--",
		"artifact", "timings.json", `{"ms":[1,2]}`, ";", "artifact", "by-shape/64x64.csv", "64,1.5", ";", "exit", "2")...)
	if r.code != exitFailed {
		t.Fatalf("submit --wait = %+v, want the job's failure", r)
	}
	id := strings.Fields(r.stdout)[0]

	r = c.run(withTask("outputs", id)...)
	if r.code != exitOK || !strings.Contains(r.stdout, "outputs: 2 files, 18 bytes") ||
		!strings.Contains(r.stdout, "by-shape/64x64.csv\t6 bytes\tsha256:") || !strings.Contains(r.stdout, string(full)[:12]) ||
		!strings.Contains(r.stdout, "environment: host/") {
		t.Fatalf("outputs = %+v", r)
	}

	to := filepath.Join(t.TempDir(), "made", "here")
	if err := os.MkdirAll(to, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(to, "timings.json"), []byte("stale"), 0o600); err != nil {
		t.Fatal(err)
	}
	r = c.run(withTask("outputs", id, "--to", to)...)
	if r.code != exitOK || !strings.Contains(r.stdout, "copied to "+to) {
		t.Fatalf("outputs --to = %+v", r)
	}
	for name, want := range map[string]string{"timings.json": `{"ms":[1,2]}`, "by-shape/64x64.csv": "64,1.5"} {
		if got, err := os.ReadFile(filepath.Join(to, filepath.FromSlash(name))); err != nil || string(got) != want {
			t.Errorf("%s = %q, %v; want %q", name, got, err, want)
		}
	}
	entries, _ := filepath.Glob(filepath.Join(to, "*.vibesys-part"))
	if len(entries) != 0 {
		t.Errorf("parts left behind: %v", entries)
	}

	r = c.run(withTask("outputs", id, "--json")...)
	var job struct {
		Outputs struct {
			Kept struct {
				Outputs []struct{ Name string } `json:"outputs"`
			} `json:"kept"`
		} `json:"outputs"`
	}
	if r.code != exitOK || json.Unmarshal([]byte(r.stdout), &job) != nil || len(job.Outputs.Kept.Outputs) != 2 {
		t.Fatalf("outputs --json = %+v", r)
	}
}

// A job that kept nothing says why and copies nothing: one without --outputs is a mistake in the
// input, and a file that changed since the job ended is refused, leaving no part of it.
func TestOutputsOfAJobThatKeptNoneSaysWhy(t *testing.T) {
	c := newOutputsCLI(t)
	plain := c.submit("artifact", "x", "y")
	c.run(withTask("wait", plain)...)
	if r := c.run(withTask("outputs", plain)...); r.code != exitInvalidInput || !strings.Contains(r.stderr, "--outputs") {
		t.Fatalf("outputs of a job without --outputs = %+v", r)
	}

	r := c.run(withTask("submit", "command", "--image", "harness", "--env", "host", "--outputs", "--wait", "HEAD", "--", "artifact", "r.json", "abc")...)
	if r.code != exitOK {
		t.Fatalf("submit = %+v", r)
	}
	id := strings.Fields(r.stdout)[0]
	c.world.Files.Put(path.Join("/state", "kv", id, "01-command", "r.json"), []byte("abd"))
	to := t.TempDir()
	if r := c.run(withTask("outputs", id, "--to", to)...); r.code == exitOK || !strings.Contains(r.stderr, "changed since") {
		t.Fatalf("outputs --to of a changed file = %+v", r)
	}
	if entries, _ := os.ReadDir(to); len(entries) != 0 {
		t.Fatalf("a refused copy left %v", entries)
	}
}
