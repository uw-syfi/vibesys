package gitrepo

import (
	"context"
	"fmt"
	"maps"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// FakeBranches is Branches over repositories described in memory: commits with parents, and
// branches at commits. It is a real implementation that the tests of the daemon use, so none of
// them depends on a developer's repository.
type FakeBranches struct {
	mu    sync.Mutex
	repos map[string]*fakeRepo
	fail  error
}

type fakeRepo struct {
	branches map[string]core.Commit
	parents  map[core.Commit][]core.Commit
	// files are the files of each commit, by path to a content label: what ChangedPaths compares.
	files map[core.Commit]map[string]string
	// head is the commit the repository's own checkout is detached at, when SetHead set one.
	head core.Commit
	// checkouts are the work trees by path. A clean one holds the files of its branch's head.
	checkouts map[string]*fakeCheckout
}

type fakeCheckout struct {
	branch string
	// at is the commit whose files and index the checkout holds.
	at    core.Commit
	dirty bool
}

var _ Branches = (*FakeBranches)(nil)

// NewFakeBranches returns a FakeBranches with no repository.
func NewFakeBranches() *FakeBranches { return &FakeBranches{repos: map[string]*fakeRepo{}} }

func (f *FakeBranches) repo(path string) *fakeRepo {
	r, ok := f.repos[path]
	if !ok {
		r = &fakeRepo{branches: map[string]core.Commit{}, files: map[core.Commit]map[string]string{}, parents: map[core.Commit][]core.Commit{}, checkouts: map[string]*fakeCheckout{}}
		f.repos[path] = r
	}
	return r
}

// AddCommit makes a commit with the given parents in the repository, which is made if it is new.
// A parent that was never added is added with no parents.
func (f *FakeBranches) AddCommit(repository string, commit core.Commit, parents ...core.Commit) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.repo(repository)
	r.parents[commit] = append([]core.Commit(nil), parents...)
	for _, p := range parents {
		if _, ok := r.parents[p]; !ok {
			r.parents[p] = nil
		}
	}
}

// SetFiles sets the files of a commit: each path with a label standing for its content. A commit
// never given files has none.
func (f *FakeBranches) SetFiles(repository string, commit core.Commit, files map[string]string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.repo(repository).files[commit] = maps.Clone(files)
}

// SetHead detaches the repository's own checkout at a commit, which the repository must have.
func (f *FakeBranches) SetHead(repository string, commit core.Commit) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.repo(repository).head = commit
}

// AddBranch makes a branch at a commit, adding the commit if the repository lacks it.
func (f *FakeBranches) AddBranch(repository, branch string, at core.Commit) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.repo(repository)
	if _, ok := r.parents[at]; !ok {
		r.parents[at] = nil
	}
	r.branches[branch] = at
}

// AddCheckout makes a work tree at path that has the branch checked out and holds its head's files.
// The branch must exist.
func (f *FakeBranches) AddCheckout(repository, path, branch string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.repo(repository)
	r.checkouts[path] = &fakeCheckout{branch: branch, at: r.branches[branch]}
}

// DirtyCheckout gives the work tree at path a local change to a tracked file.
func (f *FakeBranches) DirtyCheckout(repository, path string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.repo(repository).checkouts[path].dirty = true
}

// CheckoutState returns the commit whose files the work tree at path holds, and whether it has local
// changes, for a test that looks without going through the daemon.
func (f *FakeBranches) CheckoutState(repository, path string) (core.Commit, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c := f.repo(repository).checkouts[path]
	return c.at, c.dirty
}

// FailNextFastForward makes the next FastForward fail with err before it changes anything: a move
// that did not happen, as when the process died just before it.
func (f *FakeBranches) FailNextFastForward(err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.fail = err
}

// At returns the commit a branch is at, for a test that looks without going through the daemon.
func (f *FakeBranches) At(repository, branch string) (core.Commit, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	c, ok := f.repo(repository).branches[branch]
	return c, ok
}

// Head implements Branches.
func (f *FakeBranches) Head(_ context.Context, repository, branch string) (core.Commit, error) {
	c, ok := f.At(repository, branch)
	if !ok {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s has no branch %q", repository, branch)}
	}
	return c, nil
}

// CheckoutHead implements Branches. The checkout is the work tree added at the repository's own path,
// or else the commit SetHead gave.
func (f *FakeBranches) CheckoutHead(_ context.Context, repository string) (core.Commit, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.repo(repository)
	if c, ok := r.checkouts[repository]; ok && c.at != "" {
		return c.at, nil
	}
	if r.head != "" {
		return r.head, nil
	}
	return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s has no commit checked out", repository)}
}

// ChangedPaths implements Branches.
func (f *FakeBranches) ChangedPaths(_ context.Context, repository string, base, commit core.Commit) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.repo(repository)
	for _, c := range []core.Commit{base, commit} {
		if _, ok := r.parents[c]; !ok {
			return nil, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s lacks commit %s", repository, c)}
		}
	}
	var out []string
	a, b := r.files[base], r.files[commit]
	for p, label := range a {
		if got, ok := b[p]; !ok || got != label {
			out = append(out, p)
		}
	}
	for p := range b {
		if _, ok := a[p]; !ok {
			out = append(out, p)
		}
	}
	slices.Sort(out)
	return out, nil
}

// IsAncestor implements Branches.
func (f *FakeBranches) IsAncestor(_ context.Context, repository string, ancestor, descendant core.Commit) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.repo(repository).isAncestor(repository, ancestor, descendant)
}

func (r *fakeRepo) isAncestor(repository string, ancestor, descendant core.Commit) (bool, error) {
	for _, c := range []core.Commit{ancestor, descendant} {
		if _, ok := r.parents[c]; !ok {
			return false, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s lacks commit %s", repository, c)}
		}
	}
	seen := map[core.Commit]bool{}
	stack := []core.Commit{descendant}
	for len(stack) > 0 {
		c := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if c == ancestor {
			return true, nil
		}
		if seen[c] {
			continue
		}
		seen[c] = true
		stack = append(stack, r.parents[c]...)
	}
	return false, nil
}

// FastForward implements Branches.
func (f *FakeBranches) FastForward(_ context.Context, repository, branch string, from, to core.Commit) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := f.fail; err != nil {
		f.fail = nil
		return err
	}
	r := f.repo(repository)
	head, ok := r.branches[branch]
	if !ok {
		return &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s has no branch %q", repository, branch)}
	}
	if head == to {
		return nil
	}
	if head != from {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("branch %q is at %s, not %s", branch, head, from)}
	}
	ahead, err := r.isAncestor(repository, from, to)
	if err != nil {
		return err
	}
	if !ahead {
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("commit %s is not a descendant of %s, so branch %q cannot fast-forward to it", to, from, branch)}
	}
	if err := r.checkCheckouts(branch); err != nil {
		return err
	}
	for _, c := range r.checkouts {
		if c.branch == branch {
			c.at = to
		}
	}
	r.branches[branch] = to
	return nil
}

// CheckFastForward implements Branches.
func (f *FakeBranches) CheckFastForward(_ context.Context, repository, branch string, _ core.Commit) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	r := f.repo(repository)
	if _, ok := r.branches[branch]; !ok {
		return &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s has no branch %q", repository, branch)}
	}
	return r.checkCheckouts(branch)
}

func (r *fakeRepo) checkCheckouts(branch string) error {
	for path, c := range r.checkouts {
		if c.branch == branch && c.dirty {
			return dirtyCheckout(branch, path)
		}
	}
	return nil
}
