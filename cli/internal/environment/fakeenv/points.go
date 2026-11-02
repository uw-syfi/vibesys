package fakeenv

import (
	"context"
)

// Points are the places a script signals through, besides the gates it waits at. A test reads
// them to know what a script has done, and sets flags a script reads. MemoryGates are Points.
type Points interface {
	Gates
	// Mark records that a script reached the point and wakes whoever waits for it.
	Mark(name string)
	// Flag reports whether the test has set the flag.
	Flag(name string) bool
	// Live records that a process of a script runs, under a name, and returns the function that
	// records that it ended. It marks LiveMark(name).
	Live(name string) (ended func())
}

// LiveMark is the point that Live(name) marks once the process is recorded.
func LiveMark(name string) string { return "alive-" + name }

var _ Points = (*MemoryGates)(nil)

func (g *MemoryGates) mark(name string) chan struct{} {
	g.mu.Lock()
	defer g.mu.Unlock()
	ch, ok := g.marks[name]
	if !ok {
		ch = make(chan struct{})
		g.marks[name] = ch
	}
	return ch
}

// Mark implements Points.
func (g *MemoryGates) Mark(name string) {
	ch := g.mark(name)
	g.mu.Lock()
	defer g.mu.Unlock()
	select {
	case <-ch:
	default:
		close(ch)
	}
}

// Marked reports whether a script has marked the point.
func (g *MemoryGates) Marked(name string) bool {
	select {
	case <-g.mark(name):
		return true
	default:
		return false
	}
}

// Reached waits until a script has marked the point, and reports false if ctx ends first.
func (g *MemoryGates) Reached(ctx context.Context, name string) bool {
	select {
	case <-g.mark(name):
		return true
	case <-ctx.Done():
		return false
	}
}

// SetFlag sets a flag a script can test with unless-flag.
func (g *MemoryGates) SetFlag(name string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.flags[name] = true
}

// Flag implements Points.
func (g *MemoryGates) Flag(name string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.flags[name]
}

// Live implements Points.
func (g *MemoryGates) Live(name string) func() {
	ended := make(chan struct{})
	g.mu.Lock()
	g.lives[name] = append(g.lives[name], ended)
	g.mu.Unlock()
	g.Mark(LiveMark(name))
	return func() {
		g.mu.Lock()
		defer g.mu.Unlock()
		select {
		case <-ended:
		default:
			close(ended)
		}
	}
}

// Gone waits until every process recorded under the name has ended, once the first is recorded,
// and reports false if ctx ends first.
func (g *MemoryGates) Gone(ctx context.Context, name string) bool {
	if !g.Reached(ctx, LiveMark(name)) {
		return false
	}
	g.mu.Lock()
	ended := append([]chan struct{}(nil), g.lives[name]...)
	g.mu.Unlock()
	for _, ch := range ended {
		select {
		case <-ch:
		case <-ctx.Done():
			return false
		}
	}
	return true
}
