package environment

import (
	"context"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// Allocator makes, observes and ends a placement's standing allocations (internal/alloc): one batch
// allocation of a cluster, or one slot of a machine. There is one implementation per placement
// kind, and each passes the contract suite in allocatortest, so the daemon is written once against
// this interface. It decides nothing: whether an allocation is running, lost or due for renewal is
// the core's to decide from what Observe reports.
//
// Every method fails with core.FailureUnavailable when the backend cannot be reached. That is never
// an end: an allocation the backend could not be asked about is as it was last seen.
type Allocator interface {
	// Request asks the backend for the allocation and returns once the backend has taken the
	// request, usually before it starts. It is idempotent by spec.ID: asking again for an ID whose
	// allocation is not released returns the same handle, and makes nothing new. The backend marks
	// what it makes as a standing allocation of the ID, so Adopt finds it.
	Request(ctx context.Context, spec alloc.Spec) (alloc.Handle, error)

	// Observe says what the backend says of the allocation now: pending, running or ended. An
	// allocation that is gone from the backend, released or not, is alloc.SeenEnded, never an error.
	Observe(ctx context.Context, h alloc.Handle) (alloc.Observation, error)

	// Release ends the allocation and gives its capacity back. It is idempotent: releasing one that
	// has ended, by its release or otherwise, succeeds and does nothing.
	Release(ctx context.Context, h alloc.Handle) error

	// Adopt returns the handles of every standing allocation this allocator's backend holds that it
	// made and that has not ended, such as after the daemon restarted: exactly those, and never one
	// of the backend's other allocations.
	Adopt(ctx context.Context) ([]alloc.Handle, error)
}
