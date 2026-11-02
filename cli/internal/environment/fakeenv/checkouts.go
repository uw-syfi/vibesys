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

	mu   sync.Mutex
	open int
	made int
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
