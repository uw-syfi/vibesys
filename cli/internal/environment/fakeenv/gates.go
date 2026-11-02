package fakeenv

import (
	"context"
	"sync"
)

// MemoryGates are gates in one process. A test releases a gate with Release. They are also the
// Points a script signals through and a test observes: see points.go.
type MemoryGates struct {
	mu    sync.Mutex
	gates map[string]chan struct{}
	marks map[string]chan struct{}
	flags map[string]bool
	lives map[string][]chan struct{}
}

var _ Gates = (*MemoryGates)(nil)

// NewMemoryGates returns gates that are all closed.
func NewMemoryGates() *MemoryGates {
	return &MemoryGates{
		gates: map[string]chan struct{}{}, marks: map[string]chan struct{}{},
		flags: map[string]bool{}, lives: map[string][]chan struct{}{},
	}
}

func (g *MemoryGates) gate(name string) chan struct{} {
	g.mu.Lock()
	defer g.mu.Unlock()
	ch, ok := g.gates[name]
	if !ok {
		ch = make(chan struct{})
		g.gates[name] = ch
	}
	return ch
}

// Release opens the gate for everyone who waits at it, now and later.
func (g *MemoryGates) Release(name string) {
	ch := g.gate(name)
	g.mu.Lock()
	defer g.mu.Unlock()
	select {
	case <-ch:
	default:
		close(ch)
	}
}

// Wait implements Gates.
func (g *MemoryGates) Wait(ctx context.Context, name string) bool {
	select {
	case <-g.gate(name):
		return true
	case <-ctx.Done():
		return false
	}
}
