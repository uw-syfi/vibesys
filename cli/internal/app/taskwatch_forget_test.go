package app_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"sync"
	"testing"
	"testing/synctest"

	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

type delayedReleases struct {
	supervisor.Supervisors
	once *sync.Once
	g    *gate
}

func (s delayedReleases) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	h, e := s.Supervisors.Spawn(ctx, l)
	return delayedRelease{Handle: h, owner: s}, e
}

type delayedRelease struct {
	supervisor.Handle
	owner delayedReleases
}

func (h delayedRelease) Release(ctx context.Context) error {
	first := false
	h.owner.once.Do(func() { first = true })
	if first {
		close(h.owner.g.entered)
		select {
		case <-h.owner.g.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return h.Handle.Release(ctx)
}

// #3868: an old move replied Closed after a forget and removed the new j1
// from the lifecycle, leaving its Watch waiting on a producer nobody owned.
func TestAWatcherConvergesAfterForgettingWithAnEffectStillFinishing(t *testing.T) {
	for seed := uint64(0); seed <= 25; seed++ {
		t.Run(fmt.Sprintf("seed_%d", seed), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				r := rand.New(rand.NewPCG(seed, seed*7919))
				// Seed zero pins the CI mechanism: old Closed arrives after the new admission.
				releaseAt := 2
				if seed != 0 {
					releaseAt = r.IntN(4)
				}

				w, o := newOutage(t)
				g := newGate()
				t.Cleanup(func() {
					select {
					case <-g.release:
					default:
						close(g.release)
					}
				})
				a := o.start(delayedReleases{Supervisors: w.supervisors(), once: new(sync.Once), g: g})
				j := submitAs(t, a, callA, "exit", "0")
				o.awaitFinal(t, a, scopeA, j.ID)
				g.await(t)
				wt := watch(t, a, scopeA)
				converge(t, a, wt, o, scopeA, false)
				if releaseAt == 0 {
					close(g.release)
					synctest.Wait()
				}
				o.remove(repoA)
				if _, e := a.ForgetRepository(ctxFor(t), repoA); e != nil {
					t.Fatal(e)
				}
				o.bringBackRemoved(repoA)
				if releaseAt == 1 {
					close(g.release)
					synctest.Wait()
				}
				j = submitAs(t, a, callA, "hold", "new", ";", "exit", "0")
				// Let the old move's Closed event arrive before the new job ends.
				if releaseAt == 2 {
					close(g.release)
					synctest.Wait()
				}
				w.gates.Release("new")
				o.awaitFinal(t, a, scopeA, j.ID)
				if releaseAt == 3 {
					close(g.release)
					synctest.Wait()
				}
				converge(t, a, wt, o, scopeA, false)
			})
		})
	}
}
