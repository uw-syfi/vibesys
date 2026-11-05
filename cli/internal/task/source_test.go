package task_test

import (
	"fmt"
	"io/fs"
	"strings"
	"testing"
	"testing/fstest"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

func TestMemorySourcePassesTheSourceContract(t *testing.T) {
	var n int
	tasktest.Run(t, tasktest.Rig{
		Make: func(t *testing.T, files task.Tree) (task.Source, string, core.Commit) {
			n++
			src := task.NewMemorySource()
			commit := core.Commit(fmt.Sprintf("%040x", n))
			src.Put("/repo", commit, files)
			return src, "/repo", commit
		},
		Missing: core.Commit(strings.Repeat("f", 40)),
	})
}

// A Tree is a file system by the standard library's own conformance check.
func TestATreeIsAFileSystem(t *testing.T) {
	files := tasktest.Files("t")
	if err := fstest.TestFS(files, ".vibesys/tasks/t/check.sh", ".vibesys/tasks/t/vibesys.input.toml", "src/main.c"); err != nil {
		t.Fatal(err)
	}
	if err := fstest.TestFS(task.Tree{}); err != nil {
		t.Fatal(err)
	}
	// With links, which it follows when it opens a path and shows as links when it lists or
	// Lstats one, as a directory does.
	files[".vibesys/tasks/t/again.sh"] = task.File{Link: "check.sh"}
	files[".vibesys/tasks/t/source"] = task.File{Link: "../../../src"}
	if err := fstest.TestFS(files, ".vibesys/tasks/t/again.sh", ".vibesys/tasks/t/source"); err != nil {
		t.Fatal(err)
	}
	if data, err := fs.ReadFile(files, ".vibesys/tasks/t/source/main.c"); err != nil || string(data) != string(files["src/main.c"].Data) {
		t.Errorf("reading through a link to a directory: %q, %v", data, err)
	}
	if target, err := fs.ReadLink(files, ".vibesys/tasks/t/again.sh"); err != nil || target != "check.sh" {
		t.Errorf("ReadLink = %q, %v", target, err)
	}
	if info, err := fs.Lstat(files, ".vibesys/tasks/t/again.sh"); err != nil || info.Mode().Type() != fs.ModeSymlink {
		t.Errorf("Lstat = %v, %v; want a link", info, err)
	}
	files["out"] = task.File{Link: "../elsewhere"}
	if _, err := files.Open("out"); err == nil {
		t.Error("a link out of the tree was followed")
	}
}
