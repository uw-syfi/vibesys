// Package owned is the one way the daemon's packages start a goroutine: a Group starts it and
// Wait joins it, so nothing outlives the thing that started it. archcheck refuses a bare go
// statement in those packages.
package owned

import "sync"

// Group owns the goroutines started through it. The zero value is ready. It is safe for
// concurrent use, and Go may be called from a goroutine of the group.
type Group struct {
	wg sync.WaitGroup
}

// Go runs fn in a goroutine the group owns.
func (g *Group) Go(fn func()) { g.wg.Go(fn) }

// Wait returns when every goroutine of the group has returned.
func (g *Group) Wait() { g.wg.Wait() }
