package store

import (
	"context"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// AllocLog is the daemon's record of its standing allocations: the inputs of the allocation machine
// (internal/alloc) that changed its state, append-only and in the order they were applied. The
// daemon rebuilds its allocations by folding the log from its first input (alloc.Fold), so what the
// log holds is the allocations, and an input that is not in it did not happen.
//
// It is not a task's state, and not part of a Store: allocations are numbered across every run and
// task of the daemon, so the log is the user's, kept where the user's daemon keeps state.
//
// Every method fails with a *core.Failure marked ErrAway when the log cannot be reached, and a
// cancelled context fails with the context's error and changes nothing. A log that holds a record
// this program cannot read fails Load as core.FailureInternal, naming the record, and is never
// skipped over: a daemon that rebuilt allocations from part of a log would forget some.
type AllocLog interface {
	// Append adds an input as the next of the log, durably: once it returns, a restart finds it. An
	// input that fails is not in the log, as far as the caller may rely on, and the caller may try
	// the same input again.
	Append(ctx context.Context, in alloc.Input) error

	// Load returns every input in the order it was appended. An empty log is not an error.
	Load(ctx context.Context) ([]alloc.Input, error)
}
