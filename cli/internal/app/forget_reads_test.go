package app_test

import (
	"context"
	"strings"
	"sync/atomic"
	"testing"
	"testing/synctest"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// delayedReads holds a completed read, preserving the old store's answer even
// when a forget removes that store before its caller receives the answer.
type delayedReads struct {
	app.Stores
	kind  string
	armed atomic.Bool
	g     *gate
}

func (d *delayedReads) hold(ctx context.Context, kind string) {
	if d.kind != kind || !d.armed.CompareAndSwap(true, false) {
		return
	}
	close(d.g.entered)
	select {
	case <-d.g.release:
	case <-ctx.Done():
	}
}

func (d *delayedReads) Find(ctx context.Context, scope core.Scope) (store.Store, error) {
	st, err := d.Stores.Find(ctx, scope)
	if err != nil {
		return nil, err
	}
	return delayedReadStore{Store: st, d: d}, nil
}

func (d *delayedReads) GivenUp(ctx context.Context, scope core.Scope) ([]core.JobID, error) {
	ids, err := d.Stores.GivenUp(ctx, scope)
	if app.EffectOf(ctx) == "sweep" {
		d.hold(ctx, "recovery")
	}
	return ids, err
}

type delayedReadStore struct {
	store.Store
	d *delayedReads
}

func (s delayedReadStore) Jobs(ctx context.Context, f core.JobFilter) ([]core.Job, error) {
	jobs, err := s.Store.Jobs(ctx, f)
	if app.EffectOf(ctx) == "sweep" {
		s.d.hold(ctx, "sweep")
	}
	return jobs, err
}
func (s delayedReadStore) Job(ctx context.Context, id core.JobID) (core.Job, error) {
	job, err := s.Store.Job(ctx, id)
	if app.EffectOf(ctx) == "" {
		s.d.hold(ctx, "cancel")
	}
	return job, err
}

func TestForgettingDropsReadsAlreadyInFlight(t *testing.T) {
	for _, kind := range []string{"sweep_queued", "sweep_running", "recovery", "cancel"} {
		t.Run(kind, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				w, o := newOutage(t)
				d := &delayedReads{Stores: w.stores, kind: strings.Split(kind, "_")[0], g: newGate()}
				o.Stores = d
				t.Cleanup(func() {
					select {
					case <-d.g.release:
					default:
						close(d.g.release)
					}
				})
				a := o.start(w.supervisors())
				// Drain startup so the sweep has armed its fake slow timer before we pass it.
				synctest.Wait()
				var job core.Job
				if kind == "sweep_running" || kind == "cancel" {
					job = submitAs(t, a, callA, "hold", "old", ";", "exit", "0")
					synctest.Wait()
					if w.env.Active() != 1 {
						t.Fatal("the held command did not start")
					}
				} else {
					st, err := w.stores.Create(ctxFor(t), scopeA)
					if err != nil {
						t.Fatal(err)
					}
					out, err := st.Submit(ctxFor(t), core.Submission{Context: callA, Spec: command("exit", "0"), At: w.clock.Now()})
					if err != nil {
						t.Fatal(err)
					}
					job = out.Job
				}
				if kind == "recovery" {
					// Make the sweep mark the store unread, then recover it on the next sweep.
					o.takeAway(repoA)
					o.limits.pass()
					synctest.Wait()
					o.bringBack(repoA)
				}
				d.armed.Store(true)
				var canceled chan error
				cancelCtx, stopCancel := context.WithCancel(ctxFor(t))
				defer stopCancel()
				if kind == "cancel" {
					canceled = make(chan error, 1)
					go func() { _, err := a.Cancel(cancelCtx, callA, job.ID, "old request"); canceled <- err }()
				} else {
					o.limits.pass()
				}
				d.g.await(t)
				o.remove(repoA)
				if _, err := a.ForgetRepository(ctxFor(t), repoA); err != nil {
					t.Fatal(err)
				}
				// Finish the old run's cleanup before reusing its store. The delayed
				// read stays blocked, isolating its reply from cleanup I/O.
				synctest.Wait()
				var next core.Job
				if kind == "cancel" {
					o.bringBackRemoved(repoA)
					next = submitAs(t, a, callA, "hold", "new", ";", "exit", "0")
					synctest.Wait()
					if w.env.Active() != 1 {
						t.Fatal("the held command did not start")
					}
				}
				close(d.g.release)
				synctest.Wait()
				stopCancel()
				if got := a.Status(); kind != "cancel" && (got.Queued != 0 || got.Running != 0) {
					t.Fatalf("stale %s read resurrected forgotten work: %+v", kind, got)
				}
				o.bringBackRemoved(repoA)
				// Reusing j1 must not inherit a recovery, admission or cancellation of old j1.
				if kind == "cancel" {
					w.gates.Release("new")
				} else {
					next = submitAs(t, a, callA, "exit", "0")
				}
				o.awaitFinal(t, a, scopeA, next.ID)
				final, err := a.Job(ctxFor(t), scopeA, next.ID)
				if err != nil || final.State != core.JobSucceeded {
					t.Fatalf("new job inherited stale %s: %+v, %v", kind, final, err)
				}
				if canceled != nil {
					select {
					case <-canceled:
					case <-ctxFor(t).Done():
						t.Fatal("old cancellation never returned")
					}
				}
				wt := watch(t, a, scopeA)
				converge(t, a, wt, o, scopeA, false)
			})
		})
	}
}
