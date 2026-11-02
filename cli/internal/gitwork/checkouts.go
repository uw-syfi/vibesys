package gitwork

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Checkouts makes a clean checkout of a commit as a clone of the repository with the commit
// checked out, detached. The repository is read and never changed. Base is the directory the
// checkouts are made in, and the system's temporary directory when it is empty.
type Checkouts struct {
	Base string
}

// Checkout returns a checkout of the commit and the function that removes it. It fails with
// core.FailureNotFound for a commit the repository does not have and core.FailureUnavailable for a
// repository git cannot read or a checkout it cannot make.
func (c Checkouts) Checkout(ctx context.Context, repository string, commit core.Commit) (core.Checkout, func(), error) {
	ok, err := hasCommit(ctx, repository, commit)
	if err != nil {
		return core.Checkout{}, nil, err
	}
	if !ok {
		return core.Checkout{}, nil, noCommit(repository, commit)
	}
	parent, err := os.MkdirTemp(c.Base, "vibesys-checkout-")
	if err == nil {
		// The path commands see, with no link in it.
		parent, err = filepath.EvalSymlinks(parent)
	}
	if err != nil {
		return core.Checkout{}, nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("making a directory for the checkout: %v", err)}
	}
	var once sync.Once
	remove := func() { once.Do(func() { _ = os.RemoveAll(parent) }) }
	// The clone is one level below the directory made here, so that a cancel in the middle of the
	// clone leaves only a directory that remove clears.
	dir := filepath.Join(parent, "work")
	if _, err := git(ctx, parent, "clone", "--quiet", "--no-checkout", "--local", "--", repository, dir); err != nil {
		remove()
		return core.Checkout{}, nil, c.failed(ctx, err)
	}
	if _, err := git(ctx, dir, "checkout", "--quiet", "--detach", string(commit)); err != nil {
		remove()
		return core.Checkout{}, nil, c.failed(ctx, err)
	}
	return core.Checkout{Commit: commit, Dir: dir}, remove, nil
}

func (Checkouts) failed(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("checking out the commit: %v", err)}
}
