package production

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// Tree answers what StartChecks asks of the file system.
type Tree interface {
	// Exists says path names anything: a file, a directory or a link to either.
	Exists(path string) bool
	// IsDir says path is a directory, or a link to one.
	IsDir(path string) bool
}

// OSTree is the real file system.
type OSTree struct{}

// Exists implements Tree.
func (OSTree) Exists(path string) bool {
	_, ok := stat(path)
	return ok
}

// IsDir implements Tree.
func (OSTree) IsDir(path string) bool {
	info, ok := stat(path)
	return ok && info.IsDir()
}

func stat(path string) (os.FileInfo, bool) {
	info, err := os.Stat(path)
	return info, err == nil
}

// StartChecks is the daemon's refusal of a start that cannot work. It is a daemon.StartChecks.
type StartChecks struct {
	// Sidecars is the launcher the daemon was wired with. A launcher that refuses every launch
	// (SidecarsFor gives one when a part is missing) refuses every start, with its own reason.
	Sidecars daemon.Sidecars
	Tree     Tree
}

// CheckStart implements daemon.StartChecks. In order it refuses: a daemon with no launcher, a
// repository path that is not absolute or is not the root of a git work tree, a task name that is
// not one plain folder name, and a task the repository does not hold under .vibesys/tasks.
func (c StartChecks) CheckStart(repository, task string) error {
	switch launcher := c.Sidecars.(type) {
	case refusedSidecars:
		return &daemon.StartRefusal{Reason: "no agent can be launched: " + launcher.reason, NotReady: true}
	case NoSidecars:
		return &daemon.StartRefusal{Reason: "no agent can be launched: this build has no sidecar launcher wired in", NotReady: true}
	}
	refuse := func(format string, args ...any) error {
		return &daemon.StartRefusal{Reason: fmt.Sprintf(format, args...)}
	}
	switch {
	case !filepath.IsAbs(repository):
		return refuse("repository %q is not an absolute path", repository)
	case !c.Tree.Exists(filepath.Join(repository, ".git")):
		return refuse("repository %s is not a git work tree: it has no .git", repository)
	case !validTaskName(task):
		return refuse("task %q is not a task name: it must be one folder name under .vibesys/tasks, with no path separators and not . or ..", task)
	}
	dir := filepath.Join(repository, ".vibesys", "tasks", task)
	if !c.Tree.IsDir(dir) {
		return refuse("task %q has no directory %s", task, dir)
	}
	return nil
}

// validTaskName says a task is one plain folder name, so that joining it under .vibesys/tasks
// never leaves that folder.
func validTaskName(task string) bool {
	return task != "" && task != "." && task != ".." && !strings.ContainsAny(task, "/\\\x00")
}
