package gitrepo

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"repoctl/internal/command"
)

// HooksState says whether a repository runs the hooks in a tracked directory.
type HooksState int

const (
	// HooksActive: core.hooksPath already names the directory.
	HooksActive HooksState = iota
	// HooksInstalled: this call set core.hooksPath to the directory.
	HooksInstalled
	// HooksForeign: core.hooksPath names another directory, which this call leaves alone.
	HooksForeign
)

// EnsureHooksPath points the repository at the tracked hooks directory dir (relative to
// the working tree root) unless core.hooksPath already names another one. The setting
// lives in the repository's own config, which every linked worktree shares, and a
// relative path is resolved against each worktree's root, so one call covers a clone and
// all its worktrees. The result also returns the path in force, for the caller's message.
func EnsureHooksPath(root, dir string) (HooksState, string, error) {
	hook := filepath.Join(root, filepath.FromSlash(dir), "pre-push")
	if info, err := os.Stat(hook); err != nil || info.IsDir() {
		return HooksActive, "", fmt.Errorf("no pre-push hook at %s", hook)
	}
	out, err := command.Run(root, "git", "config", "--default", "", "--get", "core.hooksPath")
	if err != nil {
		return HooksActive, "", err
	}
	current := strings.TrimSpace(string(out))
	switch current {
	case dir:
		return HooksActive, current, nil
	case "":
		if _, err := command.Run(root, "git", "config", "--local", "core.hooksPath", dir); err != nil {
			return HooksActive, "", err
		}
		return HooksInstalled, dir, nil
	default:
		return HooksForeign, current, nil
	}
}

// CommonDir is the repository's shared git directory, the same for every worktree.
func CommonDir(root string) (string, error) {
	out, err := command.Run(root, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

// Head is the current branch (empty when detached) and commit of the working tree.
func Head(root string) (branch, sha string) {
	if out, err := command.Run(root, "git", "branch", "--show-current"); err == nil {
		branch = strings.TrimSpace(string(out))
	}
	if out, err := command.Run(root, "git", "rev-parse", "--short", "HEAD"); err == nil {
		sha = strings.TrimSpace(string(out))
	}
	return branch, sha
}

// LocalEnvVars names the environment variables git sets for a hook and reads to find the
// repository (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and the like), from git itself.
func LocalEnvVars(root string) ([]string, error) {
	out, err := command.Run(root, "git", "rev-parse", "--local-env-vars")
	if err != nil {
		return nil, err
	}
	return strings.Fields(string(out)), nil
}
