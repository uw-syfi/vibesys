package app

import (
	"context"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// Scope names the store a request is about: one task in one repository.
type Scope struct {
	Repository string
	Task       string
}

// Validate checks the scope is one a store can be made for, naming the field that is not.
func (s Scope) Validate() error {
	if !filepath.IsAbs(s.Repository) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("context.repository: %q is not an absolute path", s.Repository)}
	}
	if !taskName.MatchString(s.Task) || strings.HasPrefix(s.Task, ".") {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"context.task: %q is not a task name (1 to 128 of A-Z, a-z, 0-9, dot, underscore and hyphen, not starting with a dot)", s.Task)}
	}
	return nil
}

var taskName = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

// Stores opens the stores the daemon serves. The daemon is the only user of a store, so an
// implementation hands the same store to every Open of a scope for as long as it lives.
type Stores interface {
	// Open returns the store of the scope, creating it when it is missing, and records that the
	// scope has one, so that Known finds it after a restart.
	Open(ctx context.Context, scope Scope) (store.Store, error)

	// Known returns every scope that has a store, including one whose store cannot be reached now:
	// leaving that out would make its runs look like nobody's.
	Known(ctx context.Context) ([]Scope, error)

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
