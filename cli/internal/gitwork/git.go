package gitwork

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// git runs the git program in dir and returns its standard output. Its failure carries the
// standard error.
func git(ctx context.Context, dir string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir, "-c", "core.hooksPath=/dev/null", "-c", "advice.detachedHead=false"}, args...)...)
	cmd.Env = append(cmd.Environ(), "GIT_TERMINAL_PROMPT=0")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, fmt.Errorf("git %s: %w: %s", args[0], err, strings.TrimSpace(stderr.String()))
	}
	return stdout.Bytes(), nil
}

// hasCommit says whether the repository has the commit. It fails with core.FailureUnavailable
// when git cannot read the repository at all, and otherwise answers.
func hasCommit(ctx context.Context, repository string, commit core.Commit) (bool, error) {
	if !commit.Valid() {
		return false, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("commit: %q is not a full commit hash", commit)}
	}
	if _, err := git(ctx, repository, "rev-parse", "--git-dir"); err != nil {
		if ctx.Err() != nil {
			return false, ctx.Err()
		}
		return false, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("%s is not a repository git can read: %v", repository, err)}
	}
	if _, err := git(ctx, repository, "cat-file", "-e", string(commit)+"^{commit}"); err != nil {
		if ctx.Err() != nil {
			return false, ctx.Err()
		}
		return false, nil
	}
	return true, nil
}

func noCommit(repository string, commit core.Commit) error {
	return &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("commit %s is not a commit of %s", commit, repository)}
}
