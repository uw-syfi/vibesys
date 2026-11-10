package localalloc_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/environment/allocatortest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeslots"
	"github.com/uw-syfi/vibesys/cli/internal/environment/localalloc"
)

// The local allocator passes the contract every Allocator passes. Its backend is endless, and the
// slots it takes are as many as the contract's allocations, so every one of them starts.
func TestLocalAllocatorPassesTheAllocatorContract(t *testing.T) {
	allocatortest.Run(t, allocatortest.Contract{New: func(allocatortest.TB) allocatortest.World {
		return allocatortest.World{
			Allocator: localalloc.Allocator{Slots: fakeslots.New(8)},
			Pass:      func(time.Duration) {},
			Endless:   true,
		}
	}})
}

// An allocation of a placement with no free slot waits, and starts when another gives its slot back.
func TestAnAllocationWaitsForASlotAndTakesTheOneAnotherLeaves(t *testing.T) {
	a := localalloc.Allocator{Slots: fakeslots.New(1)}
	ctx := t.Context()
	first, err := a.Request(ctx, alloc.Spec{ID: "a1", Run: "r", Placement: "p", For: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	second, err := a.Request(ctx, alloc.Spec{ID: "a2", Run: "r", Placement: "p", For: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if o, _ := a.Observe(ctx, first); o != (alloc.SeenRunning{}) {
		t.Fatalf("the first allocation is %+v, want running", o)
	}
	if o, _ := a.Observe(ctx, second); o != (alloc.SeenPending{Reason: "waiting for a free slot of the placement"}) {
		t.Fatalf("the second allocation is %+v, want pending for a slot", o)
	}
	if err := a.Release(ctx, first); err != nil {
		t.Fatal(err)
	}
	if o, _ := a.Observe(ctx, second); o != (alloc.SeenRunning{}) {
		t.Fatalf("after the first was released the second is %+v, want running", o)
	}
}

func TestTheReferenceNamesTheAllocation(t *testing.T) {
	a := localalloc.Allocator{Slots: fakeslots.New(1)}
	h, err := a.Request(t.Context(), alloc.Spec{ID: "a7", Run: "r", Placement: "p", For: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	if id, ok := localalloc.IDOf(h.Ref); !ok || id != "a7" {
		t.Fatalf("IDOf(%q) = %q, %v, want a7", h.Ref, id, ok)
	}
	if _, ok := localalloc.IDOf("12345"); ok {
		t.Fatal("a cluster's batch job id was taken for a slot's reference")
	}
}
