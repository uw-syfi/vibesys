package production

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// Tree answers what StartChecks asks of the file system and of the repository's history.
type Tree interface {
	// Exists says path names anything: a file, a directory or a link to either.
	Exists(path string) bool
	// IsDir says path is a directory, or a link to one.
	IsDir(path string) bool
	// RealPath returns path with every symbolic link resolved, and an error when it cannot.
	RealPath(path string) (string, error)
	// HasCommit says the git work tree at repository has a commit at HEAD. A branch that was created
	// and never committed to has none. A detached HEAD that names a commit counts.
	HasCommit(repository string) (bool, error)
	// CommittedDir says dir, a slash-separated path from the root of the work tree, is a directory
	// at the commit HEAD names. The files of the working tree do not count.
	CommittedDir(repository, dir string) (bool, error)
}

// OSTree is the real file system, and git for the history.
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

// RealPath implements Tree.
func (OSTree) RealPath(path string) (string, error) { return filepath.EvalSymlinks(path) }

// HasCommit implements Tree.
func (OSTree) HasCommit(repository string) (bool, error) {
	_, err := git(repository, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() == 1 {
		return false, nil // With --verify --quiet, exit 1 means HEAD names no commit, and nothing else does.
	}
	return err == nil, err
}

// CommittedDir implements Tree.
func (OSTree) CommittedDir(repository, dir string) (bool, error) {
	out, err := git(repository, "ls-tree", "-d", "--name-only", "HEAD", "--", dir)
	return strings.TrimSpace(out) != "", err
}

// git runs git in repository, as sidecars.go does to make the agents' copies of it.
func git(repository string, args ...string) (string, error) {
	var stdout, stderr bytes.Buffer
	cmd := exec.Command("git", append([]string{"-C", repository}, args...)...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("git %s in %s: %w: %s", args[0], repository, err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}

func stat(path string) (os.FileInfo, bool) {
	info, err := os.Stat(path)
	return info, err == nil
}

// StartChecks is the daemon's refusal of a start that cannot work. It is a daemon.StartChecks.
type StartChecks struct {
	// Sidecars is the launcher the daemon was wired with.
	Sidecars daemon.Sidecars
	// Parts says whether every part a run is started with can be used now. It is asked on every
	// start, so a start refused for a missing part goes through once the install is repaired.
	Parts PartsCheck
	Tree  Tree
}

// PartsCheck is nil when every part can be used now, and otherwise says which cannot and why.
type PartsCheck interface{ Check() error }

// CheckStart implements daemon.StartChecks. In order it refuses: a daemon with no launcher, a
// repository path that is not absolute, does not exist or is not the root of a git work tree, a
// task name that is not one plain folder name, a task the repository does not hold under
// .vibesys/tasks, and a repository whose latest commit cannot feed the subagents: they work in
// copies made from it, so it must exist and hold the task. Edits to the task's files that are not
// committed are the lead's to judge, as its skill says. It returns the repository with its
// symbolic links resolved.
func (c StartChecks) CheckStart(repository, task string) (string, error) {
	if _, none := c.Sidecars.(NoSidecars); none {
		return "", &daemon.StartRefusal{Reason: "no agent can be launched: this build has no way to start agent sessions", NotReady: true}
	}
	if err := c.Parts.Check(); err != nil {
		return "", &daemon.StartRefusal{Reason: "no agent can be launched: " + err.Error(), NotReady: true}
	}
	refuse := func(format string, args ...any) (string, error) {
		return "", &daemon.StartRefusal{Reason: fmt.Sprintf(format, args...)}
	}
	if !filepath.IsAbs(repository) {
		return refuse("repository %q is not an absolute path", repository)
	}
	given := repository
	repository, err := c.Tree.RealPath(given)
	switch {
	case err != nil:
		return refuse("repository %s cannot be found: %v", given, err)
	case !c.Tree.Exists(filepath.Join(repository, ".git")):
		return refuse("repository %s is not a git work tree: it has no .git", repository)
	case !validTaskName(task):
		return refuse("task %q is not a task name: it must be one folder name under .vibesys/tasks, with no path separators and not . or ..", task)
	}
	dir := filepath.Join(repository, ".vibesys", "tasks", task)
	if !c.Tree.IsDir(dir) {
		return refuse("task %q has no directory %s", task, dir)
	}
	committed, err := c.Tree.HasCommit(repository)
	if err != nil {
		return "", err
	}
	if !committed {
		return refuse("repository %s has no commit yet: agents work on copies made from the latest commit, so commit the repository, task included, then start the run again", repository)
	}
	rel := filepath.ToSlash(filepath.Join(".vibesys", "tasks", task))
	if committed, err = c.Tree.CommittedDir(repository, rel); err != nil {
		return "", err
	} else if !committed {
		return refuse("task %q is not committed in %s: agents work on copies made from the latest commit, so run `git add %s && git commit` there, then start the run again", task, repository, rel)
	}
	return repository, nil
}

// validTaskName says a task is one plain folder name, so that joining it under .vibesys/tasks
// never leaves that folder.
func validTaskName(task string) bool {
	return task != "" && task != "." && task != ".." && !strings.ContainsAny(task, "/\\\x00")
}
