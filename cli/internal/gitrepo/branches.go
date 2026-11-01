package gitrepo

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Branches is what the daemon asks of a task's result branch, and the one change it makes to it.
// A branch is named in full, such as "vibesys/result/kv-store" (core.ResultBranch), and the
// repository is the absolute path of its root.
type Branches interface {
	// Head returns the commit the branch is at. It fails with core.FailureNotFound when the
	// repository has no such branch.
	Head(ctx context.Context, repository, branch string) (core.Commit, error)

	// IsAncestor reports whether ancestor is reachable from descendant. A commit is its own
	// ancestor. It fails with core.FailureNotFound when the repository lacks either commit.
	IsAncestor(ctx context.Context, repository string, ancestor, descendant core.Commit) (bool, error)

	// FastForward moves the branch from one commit to a descendant of it, as one atomic step, and
	// does nothing else to the repository: no checkout, no merge, no new commit. It fails with
	// core.FailureConflict when the branch is not at from any more or to is not a descendant of
	// from, and with core.FailureNotFound for a branch or a commit the repository lacks. Moving a
	// branch to the commit it is at is not an error, so a move that was interrupted after it
	// happened can be asked for again.
	FastForward(ctx context.Context, repository, branch string, from, to core.Commit) error
}

var _ Branches = System{}

// Head implements Branches.
func (System) Head(ctx context.Context, repository, branch string) (core.Commit, error) {
	out, _, err := runStatus(ctx, repository, "rev-parse", "--verify", "--quiet", "--end-of-options", "refs/heads/"+branch+"^{commit}")
	commit := core.Commit(out)
	if err != nil || !commit.Valid() {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s has no branch %q", repository, branch)}
	}
	return commit, nil
}

// IsAncestor implements Branches.
func (System) IsAncestor(ctx context.Context, repository string, ancestor, descendant core.Commit) (bool, error) {
	_, code, err := runStatus(ctx, repository, "merge-base", "--is-ancestor", string(ancestor), string(descendant))
	switch {
	case err == nil:
		return true, nil
	case code == 1:
		return false, nil
	default:
		return false, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s lacks commit %s or %s", repository, ancestor, descendant)}
	}
}

// FastForward implements Branches. The ref is updated with the commit it must be at, which git
// checks and writes under its own lock, so a branch that moved in the meantime is not overwritten.
func (s System) FastForward(ctx context.Context, repository, branch string, from, to core.Commit) error {
	head, err := s.Head(ctx, repository, branch)
	if err != nil {
		return err
	}
	if head == to {
		return nil
	}
	if head != from {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("branch %q is at %s, not %s", branch, head, from)}
	}
	ahead, err := s.IsAncestor(ctx, repository, from, to)
	if err != nil {
		return err
	}
	if !ahead {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("commit %s is not a descendant of %s, so branch %q cannot fast-forward to it", to, from, branch)}
	}
	if _, _, err := runStatus(ctx, repository, "update-ref", "-m", "vibesys-cli promote", "refs/heads/"+branch, string(to), string(from)); err != nil {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("branch %q moved while it was being updated", branch)}
	}
	return nil
}

// runStatus is run, with the exit code of a command that ran and failed (-1 when it did not run).
func runStatus(ctx context.Context, dir string, args ...string) (string, int, error) {
	out, err := run(ctx, dir, args...)
	if err == nil {
		return out, 0, nil
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return strings.TrimSpace(out), exit.ExitCode(), err
	}
	return out, -1, err
}
