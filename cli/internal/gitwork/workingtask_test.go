package gitwork_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitwork"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// WorkingTask reads every file of the task's directory, links as links and nothing generated, so the
// version taken from it is the version a commit of the same files has.
func TestWorkingTaskReadsTheWholeDirectoryAsItStands(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, task.TasksDir, "t")
	for name, text := range map[string]string{task.ManifestName: "version = \"0.2\"\n", "bin/run.sh": "echo\n", "__pycache__/x.pyc": "junk"} {
		if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), []byte(text), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("bin/run.sh", filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}
	tree, err := gitwork.CardFiles{}.WorkingTask(t.Context(), root, "t")
	if err != nil {
		t.Fatal(err)
	}
	prefix := task.TasksDir + "/t/"
	if len(tree) != 3 || tree[prefix+"bin/run.sh"].Data == nil || tree[prefix+"link"].Link != "bin/run.sh" {
		t.Fatalf("tree = %v, want the manifest, bin/run.sh and the link, and no generated file", tree)
	}
	version, err := task.Version(tree, task.TasksDir+"/t")
	if err != nil || version == "" {
		t.Fatalf("version = %q, %v", version, err)
	}
	// A task with no directory is an empty tree, which DraftOf reports as not found; a name that
	// is not a task name, and a root that is not a directory, are refused.
	if tree, err := (gitwork.CardFiles{}).WorkingTask(t.Context(), root, "absent"); err != nil || len(tree) != 0 {
		t.Errorf("absent task: %v, %v", tree, err)
	}
	if _, err := (gitwork.CardFiles{}).WorkingTask(t.Context(), root, "../t"); core.AsFailure(err).Kind != core.FailureInvalidInput {
		t.Errorf("a path as a name: %v", err)
	}
	if _, err := (gitwork.CardFiles{}).WorkingTask(t.Context(), filepath.Join(root, "nowhere"), "t"); core.AsFailure(err).Kind != core.FailureNotFound {
		t.Errorf("no root: %v", err)
	}
}
