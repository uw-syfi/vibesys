package daemontest

import (
	"context"
	"sync"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// Checkouts is the Fake of the working copies of a run's agents. It records what it was asked to
// release, and keeps the agents listed in Dirty, as a launcher keeps a copy holding work git lacks.
type Checkouts struct {
	mu       sync.Mutex
	Dirty    map[core.AgentID]bool
	released [][]core.AgentID
}

// Release implements daemon.Checkouts.
func (c *Checkouts) Release(_ context.Context, _ string, _ core.RunID, agents []core.AgentID) ([]core.Checkout, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.released = append(c.released, append([]core.AgentID(nil), agents...))
	var kept []core.Checkout
	for _, a := range agents {
		if c.Dirty[a] {
			kept = append(kept, core.Checkout{Agent: a, Location: "/state/" + string(a) + "/worktree"})
		}
	}
	return kept, nil
}

// Releases returns the agents of each call to Release, in order.
func (c *Checkouts) Releases() [][]core.AgentID {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([][]core.AgentID(nil), c.released...)
}
