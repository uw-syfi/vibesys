package app_test

import (
	"context"
	"strings"
	"testing"
	"testing/synctest"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// A sweep's read met the outage, and its failure is delivered after a Submit that succeeded on the
// restored repository. The newer successful submission must supersede that failure.
func TestAStaleSweepFailureDoesNotHoldASubmissionAfterTheStoreCameBack(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		w, o := newOutage(t)
		a := o.start(w.supervisors())
		first := submitAs(t, a, callA, "hold", "g", ";", "exit", "0")
		o.awaitIn(t, a, scopeA, first.ID, "running", func(j core.Job) bool { return j.State == core.JobRunning })
		synctest.Wait()
		o.takeAway(repoA)
		g := newGate()
		t.Cleanup(func() {
			select {
			case <-g.release:
			default:
				close(g.release)
			}
			w.gates.Release("g")
		})
		o.mu.Lock()
		o.staleRead = g
		o.mu.Unlock()
		synctest.Wait()
		o.limits.awaitSlow(t, 1)
		o.limits.pass()
		g.await(t)
		o.bringBack(repoA)
		next := submitAs(t, a, callA, "print", "next", ";", "exit", "0")
		close(g.release)
		synctest.Wait()
		v, ok := a.TaskView(ctxFor(t), scopeA, core.DefaultJudgedCommits)
		if !ok || v.Content == nil || len(v.Content.Troubles) != 0 {
			t.Fatalf("a successful Submit inherited an older outage: %+v", v)
		}
		w.gates.Release("g")
		o.awaitFinal(t, a, scopeA, first.ID)
		o.awaitFinal(t, a, scopeA, next.ID)
	})
}

// Enumerate every ordering of restore/submit and failed-reply/next-sweep that
// preserves their prerequisites. Each boundary is quiescent, including while
// the refused read is deliberately held; the scheduler cannot choose a case.
func TestNewerStoreObservationsSupersedeOlderFailedReads(t *testing.T) {
	var visit func([]string)
	visit = func(ops []string) {
		if len(ops) < 4 {
			for _, op := range []string{"restore", "submit", "reply", "sweep"} {
				used := func(want string) bool {
					for _, got := range ops {
						if got == want {
							return true
						}
					}
					return false
				}
				if used(op) || (op == "submit" && !used("restore")) || (op == "sweep" && !used("reply")) {
					continue
				}
				visit(append(append([]string{}, ops...), op))
			}
			return
		}
		t.Run(strings.Join(ops, "_"), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				w, o := newOutage(t)
				a := o.start(w.supervisors())
				first := submitAs(t, a, callA, "hold", "first", ";", "exit", "0")
				synctest.Wait()
				g := newGate()
				t.Cleanup(func() {
					select {
					case <-g.release:
					default:
						close(g.release)
					}
					w.gates.Release("first")
				})
				o.takeAway(repoA)
				o.mu.Lock()
				o.staleRead = g
				o.mu.Unlock()
				o.limits.pass()
				g.await(t)
				var next core.Job
				for _, op := range ops {
					switch op {
					case "restore":
						o.bringBack(repoA)
					case "submit":
						next = submitAs(t, a, callA, "exit", "0")
					case "reply":
						close(g.release)
					case "sweep":
						o.limits.pass()
					}
					synctest.Wait()
					if next.ID != "" {
						v, ok := a.TaskView(ctxFor(t), scopeA, core.DefaultJudgedCommits)
						if !ok || v.Content == nil || len(v.Content.Troubles) != 0 {
							t.Fatalf("after %s, a newer submission inherited an old failure: %+v", op, v)
						}
					}
				}
				w.gates.Release("first")
				o.awaitFinal(t, a, scopeA, first.ID)
				o.awaitFinal(t, a, scopeA, next.ID)
			})
		})
	}
	visit(nil)
}

// A completed Submit reply belongs to the lifetime captured before store I/O.
func (s delayedReadStore) Submit(ctx context.Context, sub core.Submission) (core.SubmitOutcome, error) {
	out, err := s.Store.Submit(ctx, sub)
	s.d.hold(ctx, "submit")
	return out, err
}

func TestForgettingDropsASubmitAdmissionAlreadyInFlight(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		w, o := newOutage(t)
		d := &delayedReads{Stores: w.stores, kind: "submit", g: newGate()}
		o.Stores = d
		a := o.start(w.supervisors())
		synctest.Wait()
		t.Cleanup(func() {
			select {
			case <-d.g.release:
			default:
				close(d.g.release)
			}
		})
		d.armed.Store(true)
		done := make(chan error, 1)
		go func() {
			_, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: callA, Spec: command("exit", "0")})
			done <- err
		}()
		d.g.await(t)
		o.remove(repoA)
		if _, err := a.ForgetRepository(ctxFor(t), repoA); err != nil {
			t.Fatal(err)
		}
		o.bringBackRemoved(repoA)
		// Recreate j1 durably, without admitting it. Only a fresh sweep may
		// discover it; the forgotten Submit must not admit this new job.
		st, err := w.stores.Create(ctxFor(t), scopeA)
		if err != nil {
			t.Fatal(err)
		}
		out, err := st.Submit(ctxFor(t), core.Submission{Context: callA, Spec: command("hold", "new", ";", "exit", "0"), At: w.clock.Now()})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { w.gates.Release("new") })
		close(d.g.release)
		select {
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
		case <-ctxFor(t).Done():
			t.Fatal("Submit did not return")
		}
		synctest.Wait()
		if s := a.Status(); s.Queued != 0 || s.Running != 0 {
			t.Fatalf("a stale admission resurrected forgotten work: %+v", s)
		}
		o.limits.pass()
		synctest.Wait()
		w.gates.Release("new")
		o.awaitFinal(t, a, scopeA, out.Job.ID)
	})
}

func (d *delayedReads) Create(ctx context.Context, scope core.Scope) (store.Store, error) {
	st, err := d.Stores.Create(ctx, scope)
	if err != nil {
		return nil, err
	}
	return delayedReadStore{Store: st, d: d}, nil
}
