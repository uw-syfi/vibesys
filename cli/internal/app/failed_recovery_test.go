package app_test

import (
	"context"
	"sync/atomic"
	"testing"
	"testing/synctest"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A store can accept a durable submission while its recovery metadata is unreadable.
type failedRecoveryStores struct {
	app.Stores
	failed atomic.Bool
}

func (s *failedRecoveryStores) GivenUp(ctx context.Context, scope core.Scope) ([]core.JobID, error) {
	if s.failed.Load() {
		return nil, errAway
	}
	return s.Stores.GivenUp(ctx, scope)
}

func TestASubmissionCannotPassOlderWorkWhenItsRecoveryReadFails(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		w, o := newOutage(t)
		st, err := w.stores.Create(ctxFor(t), scopeA)
		if err != nil {
			t.Fatal(err)
		}
		older, err := st.Submit(ctxFor(t), core.Submission{Context: callA, Spec: command("hold", "older", ";", "exit", "0"), At: w.clock.Now()})
		if err != nil {
			t.Fatal(err)
		}
		recovery := &failedRecoveryStores{Stores: w.stores}
		o.Stores = recovery
		o.takeAway(repoA)
		a := o.start(w.supervisors())
		synctest.Wait()
		o.bringBack(repoA)
		recovery.failed.Store(true)
		t.Cleanup(func() { w.gates.Release("older"); w.gates.Release("newer") })
		newer := submitAs(t, a, callA, "hold", "newer", ";", "exit", "0")
		synctest.Wait()
		if status := a.Status(); status.Running != 0 || w.env.Active() != 0 {
			t.Fatalf("a submission overtook unread older work after failed recovery: %+v", status)
		}
		recovery.failed.Store(false)
		o.limits.pass()
		synctest.Wait()
		first, err := a.Job(ctxFor(t), scopeA, older.Job.ID)
		if err != nil || first.State != core.JobRunning {
			t.Fatalf("older work = %s, %v; want running", first.State, err)
		}
		next, err := a.Job(ctxFor(t), scopeA, newer.ID)
		if err != nil || next.State != core.JobQueued {
			t.Fatalf("newer work = %s, %v; want queued", next.State, err)
		}
		w.gates.Release("older")
		o.awaitFinal(t, a, scopeA, older.Job.ID)
		w.gates.Release("newer")
		o.awaitFinal(t, a, scopeA, newer.ID)
	})
}
