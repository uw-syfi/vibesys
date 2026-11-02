// Package gitrepo answers the two questions the command line asks of git before it calls the
// daemon: which repository a directory is in, and which full commit a revision names. The daemon
// takes full commits only, so the resolving happens here, in the caller's checkout, where the
// revision means something.
package gitrepo

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Git is the questions asked of git.
type Git interface {
	// Toplevel returns the absolute path of the root of the repository that contains dir. It
	// fails with core.FailureInvalidInput when dir is in no repository.
	Toplevel(ctx context.Context, dir string) (string, error)

	// Commit returns the full commit that the revision names in the repository, such as a
	// branch, a tag, HEAD, or an abbreviated hash. It fails with core.FailureNotFound for a
	// revision the repository does not have.
	Commit(ctx context.Context, repository, revision string) (core.Commit, error)
}

// System is Git over the git program on the path.
type System struct{}

var _ Git = System{}

// Toplevel implements Git.
func (System) Toplevel(ctx context.Context, dir string) (string, error) {
	out, err := run(ctx, dir, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"%s is not in a git repository; run the command inside the repository, or give --repo", dir)}
	}
	return out, nil
}

// Commit implements Git.
func (System) Commit(ctx context.Context, repository, revision string) (core.Commit, error) {
	if strings.HasPrefix(revision, "-") {
		return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("commit: %q is not a revision", revision)}
	}
	out, err := run(ctx, repository, "rev-parse", "--verify", "--quiet", "--end-of-options", revision+"^{commit}")
	commit := core.Commit(out)
	if err != nil || !commit.Valid() {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("commit: %q is not a commit of %s", revision, repository)}
	}
	return commit, nil
}

func run(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	if err := cmd.Run(); err != nil {
		return "", err
	}
	return strings.TrimSpace(stdout.String()), nil
}
