package gitrepo

import (
	"os"
	"path/filepath"
	"testing"
)

func hookedRepo(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	initTestRepo(t, root)
	writeTestFile(t, root, ".githooks/pre-push", "#!/bin/sh\n")
	if err := os.Chmod(filepath.Join(root, ".githooks/pre-push"), 0o755); err != nil {
		t.Fatal(err)
	}
	commitTestFixture(t, root, "hooks")
	return root
}

func TestEnsureHooksPathInstallsOnceAndKeepsWhatIsThere(t *testing.T) {
	root := hookedRepo(t)
	steps := []struct {
		name  string
		setup func()
		state HooksState
		path  string
	}{
		{"first call installs", func() {}, HooksInstalled, ".githooks"},
		{"second call is quiet", func() {}, HooksActive, ".githooks"},
		{"another hooks directory is left alone", func() { gitTest(t, root, "config", "core.hooksPath", "elsewhere") }, HooksForeign, "elsewhere"},
	}
	for _, step := range steps {
		step.setup()
		state, path, err := EnsureHooksPath(root, ".githooks")
		if err != nil || state != step.state || path != step.path {
			t.Fatalf("%s: state %v, path %q, err %v", step.name, state, path, err)
		}
	}
	if got := gitTest(t, root, "config", "--get", "core.hooksPath"); got != "elsewhere" {
		t.Fatalf("core.hooksPath = %q, want it untouched", got)
	}
}

func TestEnsureHooksPathCoversEveryLinkedWorktree(t *testing.T) {
	root := hookedRepo(t)
	if _, _, err := EnsureHooksPath(root, ".githooks"); err != nil {
		t.Fatal(err)
	}
	linked := filepath.Join(t.TempDir(), "linked")
	gitTest(t, root, "worktree", "add", "-q", "-b", "other", linked)
	state, _, err := EnsureHooksPath(linked, ".githooks")
	if err != nil || state != HooksActive {
		t.Fatalf("linked worktree: state %v, err %v; the setting is shared, so it must already be active", state, err)
	}
	if _, err := os.Stat(filepath.Join(linked, ".githooks", "pre-push")); err != nil {
		t.Fatalf("a relative hooksPath needs the hook in every worktree: %v", err)
	}
}

func TestEnsureHooksPathLeavesARepositoryWithoutTheHookAlone(t *testing.T) {
	root := t.TempDir()
	initTestRepo(t, root)
	state, _, err := EnsureHooksPath(root, ".githooks")
	if err != nil || state != HooksAbsent {
		t.Fatalf("state %v, err %v; a repository with no pre-push hook has nothing to enable", state, err)
	}
	if got := gitTest(t, root, "config", "--default", "unset", "--get", "core.hooksPath"); got != "unset" {
		t.Fatalf("core.hooksPath = %q for a repository with no hook", got)
	}
}
