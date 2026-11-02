package app

import (
	"context"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// Stores opens the stores the daemon serves. The daemon is the only user of a store, so an
// implementation hands the same store to every call for a scope for as long as it lives.
type Stores interface {
	// Find returns the store of the scope, and makes nothing: only a submission or a new plan brings a
	// store into being, so a mistyped task and a mount point with no disk in it stay as they are. It fails with
	// a failure marked store.ErrNoStore when none was made, and store.ErrAway when one was made and
	// cannot be reached.
	Find(ctx context.Context, scope core.Scope) (store.Store, error)

	// Create returns the store of the scope, making it when it is missing, and records that the
	// scope has one, so that Known finds it after a restart.
	Create(ctx context.Context, scope core.Scope) (store.Store, error)

	// Known returns every scope that has a store, including one whose store cannot be reached now:
	// leaving that out would make its runs look like nobody's.
	Known(ctx context.Context) ([]core.Scope, error)

	// Forget gives a scope up for good: Known no longer has it, and nothing is touched where its
	// store was. It is idempotent.
	Forget(ctx context.Context, scope core.Scope) error

	// GivenUp returns the running jobs of the scope's store whose runs the user gave up, by removing
	// the owner record of the store, which is how a user says the place that ran them is gone for
	// good. A run of one that this daemon finds is no run of that place: it is taken back with
	// Reclaim. It makes nothing, and fails as Find does.
	GivenUp(ctx context.Context, scope core.Scope) ([]core.JobID, error)

	// Reclaim takes a job back from the given-up ones of the scope's store, durably, as a run of it
	// was found by this daemon. A job that is not given up costs nothing.
	Reclaim(ctx context.Context, scope core.Scope, job core.JobID) error

	// Close gives up every store. A daemon calls it before it lets go of the machine, so that once
	// a daemon is seen to be gone, its stores are free. Nothing may use a store after it.
	Close() error
}

// Rules returns the task-dependent decisions of internal/core that a store is opened with. Reuse
// is core.PlanCache asked about every saved result with the key: they answer a submission only
// when the cache would reuse them as they are, so a measurement with too few samples is topped up
// and not reused, and one that was topped up is. The submission lands on the latest result the
// cache used. defaultSamples is the task's default sample count.
func Rules(defaultSamples uint32) store.Rules {
	return store.Rules{
		DefaultSamples: defaultSamples,
		Reuse: func(saved []core.Result, want core.Submission) (core.JobID, bool) {
			plan, err := core.PlanCache(core.CacheQuery{
				Key:            want.Key,
				Settings:       want.Spec.Settings,
				DefaultSamples: defaultSamples,
				NoCache:        want.NoCache,
				Saved:          saved,
			})
			if err != nil || plan.Action != core.CacheReuse || len(plan.From) == 0 {
				return "", false
			}
			return plan.From[len(plan.From)-1], true
		},
	}
}
