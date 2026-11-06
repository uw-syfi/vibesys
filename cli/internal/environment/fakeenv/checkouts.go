package fakeenv

import (
	"context"
	"path"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Checkouts is a fake of the checkouts a supervisor asks for. It makes no directory: a checkout
// is a path under Root that the fake environment accepts and nothing reads. It counts the
// checkouts still open, so a test sees one that a job leaked.
type Checkouts struct {
	// Root is the directory the fake checkouts are named under.
	Root string
	// Fail fails every Checkout with this error when it is set.
	Fail error

	mu       sync.Mutex
	open     int
	made     int
	switched []core.Commit
}

// Checkout returns a checkout of the commit and the function that closes it.
func (c *Checkouts) Checkout(ctx context.Context, repository string, commit core.Commit) (core.Checkout, func(), error) {
	if err := ctx.Err(); err != nil {
		return core.Checkout{}, nil, err
	}
	if c.Fail != nil {
		return core.Checkout{}, nil, c.Fail
	}
	c.mu.Lock()
	c.open++
	c.made++
	c.mu.Unlock()
	root := c.Root
	if root == "" {
		root = "/fake-checkouts"
	}
	var once sync.Once
	closeIt := func() {
		once.Do(func() {
			c.mu.Lock()
			c.open--
			c.mu.Unlock()
		})
	}
	return core.Checkout{Commit: commit, Dir: path.Join(root, string(commit))}, closeIt, nil
}

// Switch records that the checkout now has the commit's files. Its directory stays, as a real
// switch's does.
func (c *Checkouts) Switch(ctx context.Context, _ string, _ core.Checkout, commit core.Commit) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if c.Fail != nil {
		return c.Fail
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.switched = append(c.switched, commit)
	return nil
}

// Switched returns every commit a checkout was switched to, in order.
func (c *Checkouts) Switched() []core.Commit {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]core.Commit(nil), c.switched...)
}

// Open returns how many checkouts were made and not yet closed.
func (c *Checkouts) Open() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.open
}

// Made returns how many checkouts were made.
func (c *Checkouts) Made() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.made
}
