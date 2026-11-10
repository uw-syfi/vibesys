// Package localalloc is the environment.Allocator of a placement whose capacity is slots of this
// machine, or of another one the daemon runs jobs on: an allocation there is one of the placement's
// max_jobs slots, held for as long as the allocation lives. It is running as soon as the scheduler
// grants the slot, which is at once when one is free, and it has no end, so every job fits.
//
// The slots are the daemon's scheduler's, which this package does not know: it asks for them through
// Slots, which the daemon implements over its scheduler and fakeslots implements in memory. The
// allocator itself holds nothing, so a daemon that restarts finds an allocation again only if its
// Slots does, which the daemon's own record of allocations arranges.
package localalloc

import (
	"context"
	"fmt"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// SlotState is where one allocation's slot is.
type SlotState uint8

// The states of a slot.
const (
	// SlotNone: nothing wants a slot for the allocation.
	SlotNone SlotState = iota
	// SlotWaiting: the allocation wants a slot and the placement has none free.
	SlotWaiting
	// SlotHeld: the allocation holds its slot.
	SlotHeld
)

// Slots is the capacity an Allocator takes slots of.
type Slots interface {
	// Want asks for one slot of the spec's placement for the allocation the spec names, and
	// returns once it is asked for, held or waiting. It is idempotent by ID.
	Want(ctx context.Context, spec alloc.Spec) error
	// State says where the allocation's slot is.
	State(ctx context.Context, id alloc.ID) (SlotState, error)
	// Drop gives the allocation's slot back, or stops waiting for it. It is idempotent.
	Drop(ctx context.Context, id alloc.ID) error
	// Wanted returns every allocation that holds or waits for a slot.
	Wanted(ctx context.Context) ([]alloc.ID, error)
}

// Allocator is an environment.Allocator over Slots.
type Allocator struct {
	Slots Slots
	// Owner is who this daemon is across restarts (filestore.DaemonID), the identity every
	// allocator of the daemon is made with so that Adopt finds only its daemon's allocations. The
	// slots here are the daemon's own scheduler's and belong to no other daemon, so it is kept and
	// not consulted.
	Owner string
}

var _ environment.Allocator = Allocator{}

// refPrefix starts the backend reference of every allocation of this allocator.
const refPrefix = "slot-"

func handleOf(id alloc.ID) alloc.Handle { return alloc.Handle{Alloc: id, Ref: refPrefix + string(id)} }

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// Request implements environment.Allocator.
func (a Allocator) Request(ctx context.Context, spec alloc.Spec) (alloc.Handle, error) {
	if !spec.ID.Valid() || spec.For <= 0 || spec.Placement == "" {
		return alloc.Handle{}, invalid("an allocation needs an ID, a placement and a positive time limit: %+v", spec)
	}
	if err := a.Slots.Want(ctx, spec); err != nil {
		return alloc.Handle{}, err
	}
	return handleOf(spec.ID), nil
}

// Observe implements environment.Allocator.
func (a Allocator) Observe(ctx context.Context, h alloc.Handle) (alloc.Observation, error) {
	state, err := a.Slots.State(ctx, h.Alloc)
	if err != nil {
		return nil, err
	}
	switch state {
	case SlotHeld:
		return alloc.SeenRunning{}, nil
	case SlotWaiting:
		return alloc.SeenPending{Reason: "waiting for a free slot of the placement"}, nil
	case SlotNone:
	}
	return alloc.SeenEnded{Cause: alloc.EndedOtherwise}, nil
}

// Release implements environment.Allocator.
func (a Allocator) Release(ctx context.Context, h alloc.Handle) error {
	return a.Slots.Drop(ctx, h.Alloc)
}

// Adopt implements environment.Allocator.
func (a Allocator) Adopt(ctx context.Context) ([]alloc.Handle, error) {
	ids, err := a.Slots.Wanted(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]alloc.Handle, 0, len(ids))
	for _, id := range ids {
		out = append(out, handleOf(id))
	}
	return out, nil
}

// IDOf is the allocation a backend reference names, and false for a reference of another backend.
func IDOf(ref string) (alloc.ID, bool) {
	id, ok := strings.CutPrefix(ref, refPrefix)
	return alloc.ID(id), ok && alloc.ID(id).Valid()
}
