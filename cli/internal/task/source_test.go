package task_test

import (
	"fmt"
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
}
