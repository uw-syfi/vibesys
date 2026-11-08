// Package gitrepo answers what is asked of git outside a job. The command line asks which full
// commit a revision names: the daemon takes full commits only, so that resolving happens in the
// caller's checkout, where the revision means something (CallerCommit). The daemon asks which repository a
// caller's directory belongs to (Resolver), and reads and moves result branches (Branches).
package gitrepo

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Git is the question the command line asks of git.
type Git interface {
	// Commit returns the full commit that the revision names in the repository that contains
	// dir, such as a branch, a tag, HEAD, or an abbreviated hash, as the checkout at dir reads it.
	// It fails with core.FailureNotFound for a revision the repository does not have.
	Commit(ctx context.Context, dir, revision string) (core.Commit, error)
}

// Resolver says which repository a directory belongs to. The daemon keys a task's store by what
// it answers, so every linked worktree of one repository shares one store per task.
type Resolver interface {
	// Resolve returns the canonical repository of the one that contains dir: the same for every
	// linked worktree of it and for any directory inside them. It fails with
	// core.FailureInvalidInput when dir is in no repository.
	Resolve(ctx context.Context, dir string) (core.Repository, error)
}

// Revisions is what CallerCommit asks: which commit a revision names in a work tree, and which
// repository a directory belongs to.
type Revisions interface {
	Git
	Resolver
}

// CallerCommit returns the full commit that revision names for a caller who runs in cwd and names
// repository, as the command line's --repo does. It is the one place that decides which work tree
// reads a caller's revisions.
//
// HEAD, and every revision relative to it, is each work tree's own, while branches and commits are
// shared by every linked worktree of a repository. So a revision is read in the work tree that
// contains cwd when that is a worktree of repository (both resolve to one repository), and in
// repository otherwise. An agent working in its own linked worktree, with repository naming the
// main checkout, so names its own HEAD and not the main checkout's. When cwd is in no repository,
// or in another one, the revision is read in repository, as it would be without cwd.
func CallerCommit(ctx context.Context, g Revisions, repository, cwd, revision string) (core.Commit, error) {
	dir := repository
	if cwd != "" && cwd != repository {
		ofRepository, err := g.Resolve(ctx, repository)
		if err == nil {
			if ofCwd, err := g.Resolve(ctx, cwd); err == nil && ofCwd == ofRepository {
				dir = cwd
			}
		}
	}
	return g.Commit(ctx, dir, revision)
}

// System is Git, Resolver and Branches over the git program on the path.
type System struct{}

var _ Revisions = System{}

// Resolve implements Resolver through the common git directory, which every linked worktree
// shares. A common directory called .git is the main worktree's, and the repository is the
// directory it is in; any other, such as a bare repository's, is the repository itself. Symbolic
// links are resolved, so two spellings of one path are one repository.
//
// A directory that does not exist, or that git says is in no repository, is invalid input. One
// that cannot be read, such as one on a mount that is away, or a git that cannot run, is
// unavailable: the directory may be right, and the call may work later.
func (System) Resolve(ctx context.Context, dir string) (core.Repository, error) {
	notRepo := &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
		"context.repository: %s is not in a git repository; run the command inside the repository, or give --repo", dir)}
	if !filepath.IsAbs(dir) {
		return "", notRepo
	}
	if _, err := os.Stat(dir); err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("context.repository: %s does not exist", dir)}
		}
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("context.repository: %s cannot be read: %v", dir, err)}
	}
	out, err := run(ctx, dir, "rev-parse", "--git-common-dir")
	var exit *exec.ExitError
	switch {
	case ctx.Err() != nil:
		return "", ctx.Err()
	case errors.As(err, &exit):
		return "", notRepo
	case err != nil:
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("context.repository: git cannot run in %s: %v", dir, err)}
	case out == "":
		return "", notRepo
	}
	if !filepath.IsAbs(out) {
		out = filepath.Join(dir, out)
	}
	common, err := filepath.EvalSymlinks(out)
	if err != nil {
		return "", &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("context.repository: the git directory of %s cannot be read: %v", dir, err)}
	}
	return canonical(common), nil
}

// canonical is the repository of a common git directory, an absolute path with no symbolic links.
func canonical(common string) core.Repository {
	common = filepath.Clean(common)
	if filepath.Base(common) == ".git" {
		return core.Repository(filepath.Dir(common))
	}
	return core.Repository(common)
}

// Commit implements Git.
func (System) Commit(ctx context.Context, dir, revision string) (core.Commit, error) {
	if strings.HasPrefix(revision, "-") {
		return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("commit: %q is not a revision", revision)}
	}
	out, err := run(ctx, dir, "rev-parse", "--verify", "--quiet", "--end-of-options", revision+"^{commit}")
	commit := core.Commit(out)
	if err != nil || !commit.Valid() {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("commit: %q is not a commit of the repository at %s", revision, dir)}
	}
	return commit, nil
}

// runRaw is run without trimming the output, for output whose bytes are all data.
func runRaw(ctx context.Context, dir string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir, "-c", "core.quotepath=off"}, args...)...)
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	if err := cmd.Run(); err != nil {
		return "", err
	}
	return stdout.String(), nil
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
