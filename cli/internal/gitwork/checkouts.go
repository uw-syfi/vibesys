package gitwork

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Checkouts makes a clean checkout of a commit as a clone of the repository with the commit
// checked out, detached. The repository is read and never changed. Base is the directory the
// checkouts are made in, and the system's temporary directory when it is empty.
type Checkouts struct {
	Base string
}

// Dir returns the directory the checkouts are made in.
func (c Checkouts) Dir() string {
	if c.Base != "" {
		return c.Base
	}
	return os.TempDir()
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
	parent, err := os.MkdirTemp(c.Dir(), "vibesys-checkout-")
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
	// clone leaves only a directory that remove clears. It does not force hard links (--local):
	// git links the objects when it can and copies them when the repository is on another file
	// system.
	dir := filepath.Join(parent, "work")
	if _, err := git(ctx, parent, "clone", "--quiet", "--no-checkout", "--", repository, dir); err != nil {
		remove()
		return core.Checkout{}, nil, c.failed(ctx, err)
	}
	if _, err := git(ctx, dir, "checkout", "--quiet", "--detach", string(commit)); err != nil {
		remove()
		return core.Checkout{}, nil, c.failed(ctx, err)
	}
	return core.Checkout{Commit: commit, Dir: dir}, remove, nil
}

// Switch replaces the checkout, in place, with a clean checkout of the commit: the commit's files
// and nothing else, what was built or left there before removed. The checkout's directory stays,
// so what has it mounted sees the new files. The repository is read and never changed; the
// checkout borrows its objects (objects/info/alternates), so a commit made after the checkout, on
// any branch or none, is found. It fails as Checkout does.
func (c Checkouts) Switch(ctx context.Context, repository string, checkout core.Checkout, commit core.Commit) error {
	ok, err := hasCommit(ctx, repository, commit)
	if err != nil {
		return err
	}
	if !ok {
		return noCommit(repository, commit)
	}
	common, err := git(ctx, repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
	if err != nil {
		return c.failed(ctx, err)
	}
	objects := filepath.Join(strings.TrimSpace(string(common)), "objects")
	own, err := git(ctx, checkout.Dir, "rev-parse", "--path-format=absolute", "--git-path", "objects/info/alternates")
	if err != nil {
		return c.failed(ctx, err)
	}
	if err := borrow(strings.TrimSpace(string(own)), objects); err != nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("letting the checkout read the repository's objects: %v", err)}
	}
	for _, args := range [][]string{
		{"checkout", "--quiet", "--force", "--detach", string(commit)},
		{"clean", "-ffdxq"},
	} {
		if _, err := git(ctx, checkout.Dir, args...); err != nil {
			return c.failed(ctx, err)
		}
	}
	return nil
}

// borrow adds the objects directory to the alternates file, once.
func borrow(alternates, objects string) error {
	have, err := os.ReadFile(alternates)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if slices.Contains(strings.Split(string(have), "\n"), objects) {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(alternates), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(alternates, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o644)
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintln(f, objects); err != nil {
		_ = f.Close()
		return err
	}
	return f.Close()
}

func (Checkouts) failed(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("checking out the commit: %v", err)}
}
