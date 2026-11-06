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

	// FastForward moves the branch from one commit to a descendant of it, and creates no commit.
	// Every checkout of the repository (the main one and each linked worktree) that has the branch
	// checked out moves with it: its index and files are brought to the new commit in the same step,
	// so a checkout is never left holding the old commit's files as staged changes that a commit
	// would revert. A checkout with local changes to tracked files is not touched and nothing moves.
	// It fails with core.FailureConflict when the branch is not at from any more, to is not a
	// descendant of from, or a checkout of the branch has local changes (the message names its
	// path), and with core.FailureNotFound for a branch or a commit the repository lacks. Moving a
	// branch to the commit it is at is not an error, so a move that was interrupted after it
	// happened can be asked for again.
	FastForward(ctx context.Context, repository, branch string, from, to core.Commit) error

	// CheckFastForward gives the refusal FastForward would give for a checkout that has local
	// changes (core.FailureConflict naming its path), fails with core.FailureNotFound for a branch
	// the repository lacks, and changes nothing. It lets a caller refuse before it records anything
	// that depends on the move.
	CheckFastForward(ctx context.Context, repository, branch string, to core.Commit) error
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
	checkout, held, err := checkoutOf(ctx, repository, branch)
	if err != nil {
		return err
	}
	if !held {
		return updateRef(ctx, repository, branch, from, to)
	}
	state, err := checkoutState(ctx, checkout, to)
	if err != nil {
		return err
	}
	switch state {
	case checkoutHasLocalChanges:
		return dirtyCheckout(branch, checkout)
	case checkoutAtTarget:
		// A move that stopped after it brought the files and before it moved the branch. The
		// branch is all that is left to move.
		return updateRef(ctx, repository, branch, from, to)
	}
	// The branch is checked out here and clean: git's own fast-forward moves the files, the index
	// and the branch together. Hooks are off because the daemon, not the person, makes this move.
	if _, _, err := runStatus(ctx, checkout, "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "--no-autostash", "--no-verify-signatures", "--quiet", string(to)); err != nil {
		if now, herr := s.Head(ctx, repository, branch); herr == nil && now != from && now != to {
			return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("branch %q moved while it was being updated", branch)}
		}
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("the checkout of branch %q at %s could not be brought to %s, and the branch did not move: %v", branch, checkout, to, err)}
	}
	return nil
}

// CheckFastForward implements Branches.
func (s System) CheckFastForward(ctx context.Context, repository, branch string, to core.Commit) error {
	if _, err := s.Head(ctx, repository, branch); err != nil {
		return err
	}
	checkout, held, err := checkoutOf(ctx, repository, branch)
	if err != nil || !held {
		return err
	}
	state, err := checkoutState(ctx, checkout, to)
	if err != nil {
		return err
	}
	if state == checkoutHasLocalChanges {
		return dirtyCheckout(branch, checkout)
	}
	return nil
}

func updateRef(ctx context.Context, repository, branch string, from, to core.Commit) error {
	if _, _, err := runStatus(ctx, repository, "update-ref", "-m", "vibesys-cli promote", "refs/heads/"+branch, string(to), string(from)); err != nil {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("branch %q moved while it was being updated", branch)}
	}
	return nil
}

func dirtyCheckout(branch, path string) error {
	return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
		"branch %q is checked out at %s, which has local changes to tracked files; commit or discard them there, or check out another branch, and promote again. The branch did not move",
		branch, path)}
}

// checkoutOf returns the path of the checkout that has the branch checked out, in the repository's
// main work tree or any linked one. Git allows a branch in one at a time. A work tree whose
// directory is gone is skipped: it holds no files to keep consistent.
func checkoutOf(ctx context.Context, repository, branch string) (string, bool, error) {
	out, err := run(ctx, repository, "worktree", "list", "--porcelain")
	if err != nil {
		return "", false, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("cannot list the work trees of %s: %v", repository, err)}
	}
	for _, block := range strings.Split(out, "\n\n") {
		var path string
		var onBranch, gone bool
		for _, line := range strings.Split(block, "\n") {
			switch {
			case strings.HasPrefix(line, "worktree "):
				path = strings.TrimPrefix(line, "worktree ")
			case line == "branch refs/heads/"+branch:
				onBranch = true
			case line == "prunable" || strings.HasPrefix(line, "prunable "):
				gone = true
			}
		}
		if onBranch && !gone && path != "" {
			return path, true, nil
		}
	}
	return "", false, nil
}

type checkoutKind int

const (
	checkoutClean checkoutKind = iota
	checkoutAtTarget
	checkoutHasLocalChanges
)

// checkoutState classifies a checkout of the branch: clean at its HEAD, already holding the target
// commit's files and index (a move that stopped half way), or holding local changes to tracked
// files, staged or not. Untracked and ignored files are not changes: the move leaves them be, and
// git itself refuses it when one would be overwritten.
func checkoutState(ctx context.Context, path string, to core.Commit) (checkoutKind, error) {
	unreadable := func(err error) error {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("cannot read the state of the checkout at %s: %v", path, err)}
	}
	out, err := run(ctx, path, "status", "--porcelain", "--untracked-files=no")
	if err != nil {
		return 0, unreadable(err)
	}
	if out == "" {
		return checkoutClean, nil
	}
	_, code, err := runStatus(ctx, path, "diff-index", "--quiet", string(to))
	if err == nil {
		if _, code, err = runStatus(ctx, path, "diff-index", "--cached", "--quiet", string(to)); err == nil {
			return checkoutAtTarget, nil
		}
	}
	if code == 1 {
		return checkoutHasLocalChanges, nil
	}
	return 0, unreadable(err)
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
