package app_test

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

type interleavedStores struct {
	app.Stores
	enabled *atomic.Bool
	once    *sync.Once
	write   func()
}

func (s interleavedStores) Find(ctx context.Context, scope core.Scope) (store.Store, error) {
	st, err := s.Stores.Find(ctx, scope)
	if err != nil {
		return nil, err
	}
	return interleavedStore{Store: st, owner: s}, nil
}

type interleavedStore struct {
	store.Store
	owner interleavedStores
}

func (s interleavedStore) interleave() {
	if s.owner.enabled.Load() {
		s.owner.once.Do(s.owner.write)
	}
}
func (s interleavedStore) Snapshot(ctx context.Context) (store.TaskSnapshot, error) {
	snap, err := s.Store.Snapshot(ctx)
	s.interleave()
	return snap, err
}

// Results is the preceding implementation's boundary, so the regression also
// fails there: its old jobs/results are combined with the new promotion.
func (s interleavedStore) Results(ctx context.Context, f core.ResultFilter) ([]core.Result, error) {
	rs, err := s.Store.Results(ctx, f)
	s.interleave()
	return rs, err
}
func TestTaskViewRetriesAWriteBetweenSnapshotAndDerivedReads(t *testing.T) {
	enabled := new(atomic.Bool)
	var write func()
	r := newResultsRigOver(t, newWorld(t), newBranches(), func(st app.Stores) app.Stores {
		return interleavedStores{Stores: st, enabled: enabled, once: new(sync.Once), write: func() { write() }}
	})
	r.measured(base, level)
	write = func() {
		result := storetest.SeedMeasurement(t, r.store, storetest.Measurement{Commit: cm(1), Samples: win, At: r.clock.Now()})
		storetest.SeedPromotion(t, r.store, core.Promotion{Commit: cm(1), PreviousHead: base,
			ResultJob: result.Job, Environment: result.Environment, At: r.clock.Now(), Verdict: core.Verdict{Kind: core.Better}})
		if _, err := r.store.CreatePlan(t.Context(), core.NewPlan{Title: "landed during read", Origin: core.RecordOrigin{At: r.clock.Now()}}); err != nil {
			t.Fatal(err)
		}
	}
	enabled.Store(true)
	v, ok := r.app.TaskView(ctxFor(t), scope, core.DefaultJudgedCommits)
	if !ok || v.Content == nil {
		t.Fatalf("view did not settle: %+v", v)
	}
	if len(v.Content.Jobs) != 2 || len(v.Content.Results) != 2 || len(v.Content.Promotions) != 1 || len(v.Content.Plans) != 1 {
		t.Fatalf("mixed store revisions: %+v", v.Content)
	}
}
