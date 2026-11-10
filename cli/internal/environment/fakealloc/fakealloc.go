// Package fakealloc is an environment.Allocator that keeps a backend of standing allocations in
// memory, for tests: an allocation waits a scripted number of looks, then runs until its time limit
// by a clock the test moves, a release, or a cancel the test makes as someone outside the CLI. The
// test can also make the backend unreachable for a while, and plant allocations the allocator did
// not make. It passes the contract in allocatortest.
package fakealloc

import (
	"context"
	"fmt"
	"slices"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Options shape the backend.
type Options struct {
	// Looks is how many Observes an allocation waits before it starts: zero starts it at the first.
	Looks int
	// Endless gives the allocations no time limit, as the slots of a machine have none.
	Endless bool
	// Node is where they run.
	Node string
	// Reason is what a waiting allocation says it waits for.
	Reason string
	// MovingEstimate has a waiting allocation say when it expects to start, a minute later at each
	// look, as Slurm's estimate of a queued job moves while it waits.
	MovingEstimate bool
	// Queued keeps every allocation waiting until OpenQueue, however often it is looked at; Looks
	// counts from then. A test that must see an allocation still waiting after something that may or
	// may not look at it, such as a restart, says when it starts instead of counting looks.
	Queued bool
}

// FakeAllocator is the backend and the allocator over it. Its methods are safe for concurrent use.
type FakeAllocator struct {
	clock clock.Clock
	opts  Options

	mu          sync.Mutex
	unreachable bool
	open        bool    // OpenQueue was called.
	allocs      []*held // In the order they were made.
	made        int
}

// held is one allocation the backend holds or held.
type held struct {
	handle      alloc.Handle
	spec        alloc.Spec
	standing    bool // Made by the allocator, not planted.
	looks       int
	startedAt   time.Time // Zero until it started.
	endedAt     time.Time // Zero until it ended.
	cause       alloc.EndCause
	requestedAt time.Time
}

var _ environment.Allocator = (*FakeAllocator)(nil)

// New is a backend that holds nothing, whose time is the clock's.
func New(c clock.Clock, o Options) *FakeAllocator {
	if o.Reason == "" {
		o.Reason = "Priority"
	}
	if o.Node == "" {
		o.Node = "node-a"
	}
	return &FakeAllocator{clock: c, opts: o}
}

func unavailable() error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "the fake backend is unreachable"}
}

// Request implements environment.Allocator.
func (f *FakeAllocator) Request(_ context.Context, s alloc.Spec) (alloc.Handle, error) {
	if !s.ID.Valid() || s.For <= 0 {
		return alloc.Handle{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("an allocation needs an ID and a positive time limit: %+v", s)}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.unreachable {
		return alloc.Handle{}, unavailable()
	}
	now := f.clock.Now()
	for _, h := range f.allocs {
		if h.standing && h.spec.ID == s.ID && !f.over(h, now) {
			return h.handle, nil
		}
	}
	f.allocs = append(f.allocs, f.newHeld(s, true, now))
	return f.allocs[len(f.allocs)-1].handle, nil
}

func (f *FakeAllocator) newHeld(s alloc.Spec, standing bool, now time.Time) *held {
	f.made++
	return &held{handle: alloc.Handle{Alloc: s.ID, Ref: fmt.Sprintf("fake-%d", f.made)}, spec: s, standing: standing, requestedAt: now}
}

// over says whether the allocation has ended by now, ending it by its time limit first.
func (f *FakeAllocator) over(h *held, now time.Time) bool {
	if h.endedAt.IsZero() && !h.startedAt.IsZero() && !f.opts.Endless {
		if limit := h.startedAt.Add(h.spec.For); !now.Before(limit) {
			h.endedAt, h.cause = limit, alloc.EndedByTimeLimit
		}
	}
	return !h.endedAt.IsZero()
}

func (f *FakeAllocator) find(handle alloc.Handle) *held {
	for _, h := range f.allocs {
		if h.handle == handle {
			return h
		}
	}
	return nil
}

// Observe implements environment.Allocator.
func (f *FakeAllocator) Observe(_ context.Context, handle alloc.Handle) (alloc.Observation, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.unreachable {
		return nil, unavailable()
	}
	now := f.clock.Now()
	h := f.find(handle)
	if h == nil {
		// A backend forgets what ended long ago; what it does not know is gone.
		return alloc.SeenEnded{Cause: alloc.EndedOtherwise}, nil
	}
	if f.over(h, now) {
		return alloc.SeenEnded{Cause: h.cause, At: h.endedAt}, nil
	}
	if h.startedAt.IsZero() {
		if f.opts.Queued && !f.open {
			return alloc.SeenPending{Reason: f.opts.Reason}, nil
		}
		if h.looks < f.opts.Looks {
			h.looks++
			seen := alloc.SeenPending{Reason: f.opts.Reason}
			if f.opts.MovingEstimate {
				seen.EstimatedStart = h.requestedAt.Add(time.Duration(h.looks) * time.Minute)
			}
			return seen, nil
		}
		h.startedAt = now
	}
	r := alloc.SeenRunning{Node: f.opts.Node, StartedAt: h.startedAt}
	if !f.opts.Endless {
		r.EndsAt = h.startedAt.Add(h.spec.For)
	}
	return r, nil
}

// Release implements environment.Allocator.
func (f *FakeAllocator) Release(_ context.Context, handle alloc.Handle) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.unreachable {
		return unavailable()
	}
	now := f.clock.Now()
	if h := f.find(handle); h != nil && !f.over(h, now) {
		h.endedAt, h.cause = now, alloc.EndedOtherwise
	}
	return nil
}

// Adopt implements environment.Allocator.
func (f *FakeAllocator) Adopt(context.Context) ([]alloc.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.unreachable {
		return nil, unavailable()
	}
	now := f.clock.Now()
	var out []alloc.Handle
	for _, h := range f.allocs {
		if h.standing && !f.over(h, now) {
			out = append(out, h.handle)
		}
	}
	return out, nil
}

// OpenQueue lets the allocations of a Queued backend start, as they would once the backend has
// room: each starts at a look after this one, Looks counted from now.
func (f *FakeAllocator) OpenQueue() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.open = true
}

// SetUnreachable makes the backend unreachable, or reachable again. What it holds is unchanged,
// though time goes on: an allocation reaches its time limit while it cannot be asked.
func (f *FakeAllocator) SetUnreachable(unreachable bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.unreachable = unreachable
}

// CancelOutside ends the allocation as an administrator's cancel would.
func (f *FakeAllocator) CancelOutside(handle alloc.Handle) {
	f.mu.Lock()
	defer f.mu.Unlock()
	now := f.clock.Now()
	if h := f.find(handle); h != nil && !f.over(h, now) {
		h.endedAt, h.cause = now, alloc.EndedOtherwise
	}
}

// Plant makes an allocation on the backend that the allocator did not make, such as the user's own
// batch job, which Adopt never returns.
func (f *FakeAllocator) Plant(s alloc.Spec) alloc.Handle {
	f.mu.Lock()
	defer f.mu.Unlock()
	h := f.newHeld(s, false, f.clock.Now())
	f.allocs = append(f.allocs, h)
	return h.handle
}

// Duplicate makes a second allocation of the spec as the allocator's own, beside one it holds for
// the same ID: what a request whose answer was lost, and that landed after it was asked again,
// leaves. Adopt returns both.
func (f *FakeAllocator) Duplicate(s alloc.Spec) alloc.Handle {
	f.mu.Lock()
	defer f.mu.Unlock()
	h := f.newHeld(s, true, f.clock.Now())
	f.allocs = append(f.allocs, h)
	return h.handle
}

// Live is the handles of every allocation the backend holds now, made by the allocator or not, in
// the order they were made.
func (f *FakeAllocator) Live() []alloc.Handle {
	f.mu.Lock()
	defer f.mu.Unlock()
	now := f.clock.Now()
	var out []alloc.Handle
	for _, h := range f.allocs {
		if !f.over(h, now) {
			out = append(out, h.handle)
		}
	}
	return slices.Clip(out)
}
