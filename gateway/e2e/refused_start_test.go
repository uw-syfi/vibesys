//go:build e2e

package e2e

import (
	"context"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// A start for a repository that has no such task is refused at once, naming the missing directory,
// and makes no run. It used to return a receipt, and the run failed later.
func TestAStartForAMissingTaskIsRefusedBeforeAnyRunExists(t *testing.T) {
	r := newRig(t)
	repository := t.TempDir()
	if out, err := exec.Command("git", "init", "-q", repository).CombinedOutput(); err != nil {
		t.Fatalf("git init: %v\n%s", err, out)
	}
	r.serve()

	ctx, cancel := context.WithTimeout(context.Background(), bound)
	defer cancel()
	cmd := exec.CommandContext(ctx, r.clientBinary, "--root", r.root, "run", "start", "--repository", repository, "--task", "nope", "--brief", "x")
	cmd.Env = r.env
	out, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatalf("run start for a missing task succeeded:\n%s", out)
	}
	if want := filepath.Join(repository, ".vibesys", "tasks", "nope"); !strings.Contains(string(out), want) {
		t.Fatalf("run start printed %q, want it to name %s", out, want)
	}
}
