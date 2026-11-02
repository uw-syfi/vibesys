package app_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"slices"
	"sync"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// faultPlan cuts off one call among the ones a promotion makes to the repository and the store,
// counted in the order they happen. A call cut off before fails without doing anything; one cut
// off after does its work and then fails, which is how a process that dies right after a write
// looks to the state it leaves. Nothing else fails, so a run is the same for the same plan.
type faultPlan struct {
	mu    sync.Mutex
	calls int
	at    int
	after bool
	trace []string
}

var errCutOff = &core.Failure{Kind: core.FailureInternal, Message: "cut off"}

// call counts the call. It returns whether to cut it off, and whether the work is done first.
func (p *faultPlan) call(name string) (cut, after bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	i := p.calls
	p.calls++
	p.trace = append(p.trace, name)
	return i == p.at, p.after
}

// wrap runs work under the plan. A read is never cut off after: it has no work to do.
func wrap[T any](p *faultPlan, name string, read bool, work func() (T, error)) (T, error) {
	cut, after := p.call(name)
	var zero T
	if cut && (!after || read) {
		return zero, errCutOff
	}
	v, err := work()
	if err == nil && cut {
		return zero, errCutOff
	}
	return v, err
}

type faultyBranches struct {
	gitrepo.Branches
	plan *faultPlan
}

func (f faultyBranches) Head(ctx context.Context, repo, branch string) (core.Commit, error) {
	return wrap(f.plan, "Head", true, func() (core.Commit, error) { return f.Branches.Head(ctx, repo, branch) })
}

func (f faultyBranches) IsAncestor(ctx context.Context, repo string, a, d core.Commit) (bool, error) {
	return wrap(f.plan, "IsAncestor", true, func() (bool, error) { return f.Branches.IsAncestor(ctx, repo, a, d) })
}

func (f faultyBranches) FastForward(ctx context.Context, repo, branch string, from, to core.Commit) error {
	_, err := wrap(f.plan, "FastForward", false, func() (struct{}, error) {
		return struct{}{}, f.Branches.FastForward(ctx, repo, branch, from, to)
	})
	return err
}

type faultyStores struct {
	app.Stores
	plan *faultPlan
}

func (f faultyStores) Open(ctx context.Context, s app.Scope) (store.Store, error) {
	st, err := f.Stores.Open(ctx, s)
	return faultyStore{Store: st, plan: f.plan}, err
}

type faultyStore struct {
	store.Store
	plan *faultPlan
}

func (f faultyStore) Promotions(ctx context.Context) ([]core.Promotion, error) {
	return wrap(f.plan, "Promotions", true, func() ([]core.Promotion, error) { return f.Store.Promotions(ctx) })
}

func (f faultyStore) RecordPromotion(ctx context.Context, p core.Promotion) error {
	_, err := wrap(f.plan, "RecordPromotion", false, func() (struct{}, error) { return struct{}{}, f.Store.RecordPromotion(ctx, p) })
	return err
}

// The state a daemon leaves behind, whatever cut it off, is one it recovers from: the branch is
// never ahead of the record, the record chains from the head the run started at, and a restart
// completes whatever was recorded, so the branch ends at the last recorded promotion. This is run
// for a promotion cut off at every call it makes, before and after, in a run of promotions with
// seeded measurements. Core's own test cuts the sequence off several times in a row.
func TestAPromotionCutOffAtAnyCallIsCompletedByTheNextDaemon(t *testing.T) {
	const promotions = 3
	for seed := range uint64(6) {
		rng := rand.New(rand.NewPCG(seed, 99))
		// One promotion makes about ten calls (reads, the record, the move), so cutting at 0..14 in
		// each of the three promotions covers every call of each, and calls past the end cut nothing.
		for at := 0; at < 5*promotions; at++ {
			for _, after := range []bool{false, true} {
				t.Run(fmt.Sprintf("seed %d call %d after %v", seed, at, after), func(t *testing.T) {
					w := newWorld(t)
					fake := newBranches()
					plan := &faultPlan{at: -1}
					r := newResultsRigWith(t, w, fake,
						func(s app.Stores) app.Stores { return faultyStores{s, plan} },
						func(b gitrepo.Branches) gitrepo.Branches { return faultyBranches{b, plan} })
					r.measured(base, level)
					plan.mu.Lock()
					plan.at, plan.after = at, after
					plan.mu.Unlock()

					for n := 1; n <= promotions; n++ {
						r.measured(cm(n), scaled(rng, 1+0.3*float64(n)))
						_, _ = r.promote(cm(n)) // a cut call is an error, a refusal an answer: neither may break the state
						checkConsistent(t, r, fmt.Sprintf("after promotion %d", n))
					}

					// A new daemon on the same state, which nothing cuts off: it completes what was recorded.
					plan.mu.Lock()
					plan.at = -1
					plan.mu.Unlock()
					newResultsRigOver(t, w, fake, nil)
					checkConsistent(t, r, "after the restart")
					recorded := r.recorded()
					want := base
					if len(recorded) > 0 {
						want = recorded[len(recorded)-1].Commit
					}
					if r.head() != want {
						t.Fatalf("after the restart the branch is at %.7s, want the last recorded promotion %.7s", r.head(), want)
					}
					seen := map[core.Commit]bool{}
					for _, p := range recorded {
						if seen[p.Commit] {
							t.Fatalf("%.7s was promoted twice: %+v", p.Commit, recorded)
						}
						seen[p.Commit] = true
					}
				})
			}
		}
	}
}

// scaled returns five samples around the base level times factor, with a seeded spread small
// enough that a 30% step is always better.
func scaled(rng *rand.Rand, factor float64) []float64 {
	out := make([]float64, 5)
	for i := range out {
		out[i] = 100 * factor * (1 + (rng.Float64()-0.5)*0.02)
	}
	return out
}

// checkConsistent states the invariant that holds after every call, cut off or not.
func checkConsistent(t *testing.T, r *resultsRig, when string) {
	t.Helper()
	known := []core.Commit{base}
	previous := base
	for _, p := range r.recorded() {
		if p.PreviousHead != previous {
			t.Fatalf("%s: the record does not chain from %.7s: %+v", when, previous, r.recorded())
		}
		previous = p.Commit
		known = append(known, p.Commit)
	}
	if !slices.Contains(known, r.head()) {
		t.Fatalf("%s: the branch is at %.7s, ahead of the record %v", when, r.head(), known)
	}
}
