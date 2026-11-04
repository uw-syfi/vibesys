package app_test

import (
	"context"
	"testing"
	"testing/synctest"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// closingSubmitStores closes the App after the durable write, before returning its answer.
type closingSubmitStores struct {
	app.Stores
	closeApp func()
}

func (s *closingSubmitStores) Create(ctx context.Context, scope core.Scope) (store.Store, error) {
	st, err := s.Stores.Create(ctx, scope)
	return closingSubmitStore{Store: st, owner: s}, err
}

type closingSubmitStore struct {
	store.Store
	owner *closingSubmitStores
}

func (s closingSubmitStore) Submit(ctx context.Context, sub core.Submission) (core.SubmitOutcome, error) {
	out, err := s.Store.Submit(ctx, sub)
	if err == nil {
		s.owner.closeApp()
	}
	return out, err
}

func TestSubmitReturnsDurableResultWhenTheAppCloses(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		w, o := newOutage(t)
		stores := &closingSubmitStores{Stores: w.stores}
		o.Stores = stores
		a := o.start(w.supervisors())
		stores.closeApp = a.Close
		out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: callA, Spec: command("exit", "0")})
		if err != nil {
			t.Fatalf("durable submission returned an error: %v", err)
		}
		st, err := w.stores.Find(ctxFor(t), scopeA)
		if err != nil {
			t.Fatal(err)
		}
		jobs, err := st.Jobs(ctxFor(t), core.JobFilter{})
		if err != nil || len(jobs) != 1 || jobs[0].ID != out.Job.ID {
			t.Fatalf("durable result %+v, stored jobs %+v, error %v", out, jobs, err)
		}
	})
}
