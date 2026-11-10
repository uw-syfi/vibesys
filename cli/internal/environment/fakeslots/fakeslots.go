// Package fakeslots is the slots of a placement in memory, for tests: localalloc.Slots with a fixed
// number of slots, granted in the order they were asked for.
package fakeslots

import (
	"context"
	"slices"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/environment/localalloc"
)

// Slots is a placement of a fixed number of slots. Its methods are safe for concurrent use.
type Slots struct {
	capacity int

	mu      sync.Mutex
	held    []alloc.ID
	waiting []alloc.ID
}

var _ localalloc.Slots = (*Slots)(nil)

// New is a placement of capacity slots, all free.
func New(capacity int) *Slots { return &Slots{capacity: capacity} }

// Want implements localalloc.Slots.
func (s *Slots) Want(_ context.Context, spec alloc.Spec) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if slices.Contains(s.held, spec.ID) || slices.Contains(s.waiting, spec.ID) {
		return nil
	}
	s.waiting = append(s.waiting, spec.ID)
	s.grant()
	return nil
}

// grant gives free slots to the allocations that wait, oldest first.
func (s *Slots) grant() {
	for len(s.held) < s.capacity && len(s.waiting) > 0 {
		s.held = append(s.held, s.waiting[0])
		s.waiting = s.waiting[1:]
	}
}

// State implements localalloc.Slots.
func (s *Slots) State(_ context.Context, id alloc.ID) (localalloc.SlotState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch {
	case slices.Contains(s.held, id):
		return localalloc.SlotHeld, nil
	case slices.Contains(s.waiting, id):
		return localalloc.SlotWaiting, nil
	}
	return localalloc.SlotNone, nil
}

// Drop implements localalloc.Slots.
func (s *Slots) Drop(_ context.Context, id alloc.ID) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.held = slices.DeleteFunc(s.held, func(x alloc.ID) bool { return x == id })
	s.waiting = slices.DeleteFunc(s.waiting, func(x alloc.ID) bool { return x == id })
	s.grant()
	return nil
}

// Wanted implements localalloc.Slots.
func (s *Slots) Wanted(context.Context) ([]alloc.ID, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append(slices.Clone(s.held), s.waiting...), nil
}
