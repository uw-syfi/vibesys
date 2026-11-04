package doctor

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// probeTimeout bounds a version probe or a git query; repairs have no bound, they are installs.
const probeTimeout = 5 * time.Second

// OSHost is the Host for the real machine, rooted at a repository.
type OSHost struct {
	Root string
	// Plan answers PlanChanges; the command wires the planner of the gate in. Nil means every
	// change can be planned.
	Plan func() error
	// Stdout and Stderr receive the output of repairs.
	Stdout, Stderr *os.File
}

// Getenv implements Host.
func (h OSHost) Getenv(name string) string { return os.Getenv(name) }

// Exists implements Host.
func (h OSHost) Exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// ReadFile implements Host.
func (h OSHost) ReadFile(path string) ([]byte, error) { return os.ReadFile(path) }

// Run implements Host.
func (h OSHost) Run(dir string, argv ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), probeTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Dir = dir
	var buf bytes.Buffer
	cmd.Stdout, cmd.Stderr = &buf, &buf
	err := cmd.Run()
	return buf.String(), err
}

// Filesystem implements Host.
func (h OSHost) Filesystem(path string) (Filesystem, error) { return filesystemOf(path) }

// GitConfig implements Host.
func (h OSHost) GitConfig(key string) string {
	out, err := h.Run(h.Root, "git", "config", "--get", key)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

// LinkedWorktree implements Host: in a linked worktree the git directory differs from the common
// one.
func (h OSHost) LinkedWorktree() bool {
	out, err := h.Run(h.Root, "git", "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
	if err != nil {
		return false
	}
	lines := strings.Fields(out)
	return len(lines) == 2 && lines[0] != lines[1]
}

// PlanChanges implements Host.
func (h OSHost) PlanChanges() error {
	if h.Plan == nil {
		return nil
	}
	return h.Plan()
}

// Execute implements Host.
func (h OSHost) Execute(a Action) error {
	switch a.Kind {
	case ActionRun:
		cmd := exec.Command(a.Argv[0], a.Argv[1:]...)
		cmd.Dir = a.Dir
		cmd.Stdout, cmd.Stderr = h.Stdout, h.Stderr
		return cmd.Run()
	case ActionRemoveTree:
		return os.RemoveAll(a.Path)
	case ActionMakeDirectory:
		return os.MkdirAll(a.Path, 0o700)
	case ActionSetGitConfig:
		_, err := h.Run(h.Root, "git", "config", a.Key, a.Value)
		return err
	}
	return fmt.Errorf("unknown action %q", a.Kind)
}

// nearestExisting returns path, or its nearest parent that exists, with symlinks resolved.
func nearestExisting(path string) (string, error) {
	path = filepath.Clean(path)
	for {
		if _, err := os.Stat(path); err == nil {
			return filepath.EvalSymlinks(path)
		}
		parent := filepath.Dir(path)
		if parent == path {
			return "", fmt.Errorf("no existing parent of %s", path)
		}
		path = parent
	}
}
