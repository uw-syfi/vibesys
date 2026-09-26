package gitrepo

import (
	"fmt"
	"os"
	"strings"

	"repoctl/internal/command"
)

// Snapshot checks out revision in a temporary detached worktree. The caller
// must invoke the returned cleanup function when it is done reading the tree.
func Snapshot(root, revision string) (string, func(), error) {
	if strings.Trim(revision, "0") == "" {
		return "", func() {}, nil
	}
	tmp, err := os.MkdirTemp("", "repoctl-policy-snapshot-")
	if err != nil {
		return "", nil, err
	}
	if err := os.Remove(tmp); err != nil {
		return "", nil, err
	}
	if _, err := command.Run(root, "git", "worktree", "add", "--detach", tmp, revision); err != nil {
		_ = os.RemoveAll(tmp)
		return "", nil, fmt.Errorf("create policy snapshot at %s: %w", revision, err)
	}
	cleanup := func() {
		_, _ = command.Run(root, "git", "worktree", "remove", "--force", tmp)
		_ = os.RemoveAll(tmp)
	}
	return tmp, cleanup, nil
}
