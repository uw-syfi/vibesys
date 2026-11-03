package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// frontierWorld is a set of measured commits and a frontier that admissions are applied to, as a
// store would apply them.
type frontierWorld struct {
	t        testing.TB
	results  []core.Result
	points   map[core.Commit][]float64
	frontier core.Frontier
	minute   int
}

func newFrontierWorld(t testing.TB) *frontierWorld {
	return &frontierWorld{t: t, points: map[core.Commit][]float64{}}
}

// measure saves a measurement of commit n at the point, and returns the commit.
func (w *frontierWorld) measure(n int, point []float64, options ...func(*core.Result)) core.Commit {
	w.minute++
	c := commitN(n)
	w.results = append(w.results, paretoMeasurement(fmt.Sprintf("j%d", len(w.results)+1), c, point, w.minute, options...))
	w.points[c] = point
	return c
}

func (w *frontierWorld) decide(c core.Commit) core.FrontierDecision {
	w.t.Helper()
	w.minute++
	d, err := core.DecideFrontier(core.FrontierRequest{Candidate: c, Frontier: w.frontier, Results: w.results, At: at(w.minute)})
	if err != nil {
		w.t.Fatalf("DecideFrontier(%.7s): %v", c, err)
	}
	return d
}

// add decides and applies an admission as a store does: against the revision it was decided on.
func (w *frontierWorld) add(c core.Commit) core.FrontierDecision {
	w.t.Helper()
	d := w.decide(c)
	if a, ok := d.(core.FrontierAdmission); ok && !a.AlreadyMember() {
		if a.From() != w.frontier.Revision || !a.Valid() {
			w.t.Fatalf("an admission from revision %d, valid %v, to a frontier at %d", a.From(), a.Valid(), w.frontier.Revision)
		}
		w.frontier = core.Frontier{Revision: w.frontier.Revision + 1, Members: a.Members()}
	}
	return d
}

func (w *frontierWorld) commits() []core.Commit {
	out := make([]core.Commit, len(w.frontier.Members))
	for i, m := range w.frontier.Members {
		out[i] = m.Commit
	}
	return out
}

func refusalKinds(d core.FrontierDecision) []core.FrontierRefusalKind {
	refusal, ok := d.(core.FrontierRefusal)
	if !ok {
		return nil
	}
	var out []core.FrontierRefusalKind
	for _, r := range refusal.Reasons {
		out = append(out, r.Kind)
	}
	return out
}

// Whatever is added in whatever order, no member beats another and no two are within noise of
// each other: every later member is a tradeoff against every earlier one, by the comparison
// compare makes. A commit is admitted exactly when no member beats it or matches it, and it evicts
// exactly the members it beats.
func TestTheFrontierHoldsOnlyTradeoffs(t *testing.T) {
	r := newRand(101)
	for range properties / 4 {
		w := newFrontierWorld(t)
		for n := range 1 + r.IntN(12) {
			c := w.measure(n+1, gridPoint(r))
			before := w.commits()
			d := w.add(c)
			var wantEvicted []core.Commit
			refused := false
			for _, m := range before {
				switch dominance(w.points[c], w.points[m]) {
				case core.Better:
					wantEvicted = append(wantEvicted, m)
				case core.Worse, core.WithinNoise:
					refused = true
				}
			}
			switch d := d.(type) {
			case core.FrontierAdmission:
				if refused {
					t.Fatalf("%v admitted over %v", w.points[c], before)
				}
				if !slices.Equal(d.Evicted(), wantEvicted) || d.Added().Commit != c {
					t.Fatalf("evicted %v, want %v", d.Evicted(), wantEvicted)
				}
			case core.FrontierRefusal:
				if !refused {
					t.Fatalf("%v refused by %v: %+v", w.points[c], before, d)
				}
				if !slices.Equal(w.commits(), before) {
					t.Fatal("a refusal changed the frontier")
				}
			}
			members := w.commits()
			for i, later := range members {
				for _, earlier := range members[:i] {
					got, err := core.CompareCommits(core.CompareRequest{Commit: later, Against: earlier, Results: w.results})
					if err != nil || got.Verdict.Kind != core.Tradeoff {
						t.Fatalf("members %v and %v: %+v, %v; want a tradeoff", w.points[later], w.points[earlier], got.Verdict, err)
					}
				}
			}
		}
	}
}

// For distinct points the frontier ends as the Pareto set of everything added, whatever the order
// the commits were added in.
func TestTheFrontierIsTheParetoSetInAnyOrder(t *testing.T) {
	r := newRand(102)
	for range properties / 4 {
		var points [][]float64
		for len(points) < 2+r.IntN(10) {
			p := gridPoint(r)
			if !slices.ContainsFunc(points, func(q []float64) bool { return slices.Equal(p, q) }) {
				points = append(points, p)
			}
		}
		var want []core.Commit
		for i, p := range points {
			if !slices.ContainsFunc(points, func(q []float64) bool { return dominance(q, p) == core.Better }) {
				want = append(want, commitN(i+1))
			}
		}
		for range 3 {
			w := newFrontierWorld(t)
			for i, p := range points {
				w.measure(i+1, p)
			}
			order := r.Perm(len(points))
			for _, i := range order {
				w.add(commitN(i + 1))
			}
			got := w.commits()
			slices.Sort(got)
			if !slices.Equal(got, want) {
				t.Fatalf("points %v added in order %v: frontier %v, want %v", points, order, got, want)
			}
		}
	}
}

func TestAMemberIsAdmittedAgainWithNothingChanged(t *testing.T) {
	w := newFrontierWorld(t)
	c := w.measure(1, []float64{200, 100, 100})
	w.add(c)
	before := w.frontier
	d := w.add(c)
	a, ok := d.(core.FrontierAdmission)
	if !ok || !a.AlreadyMember() || len(a.Evicted()) != 0 || !slices.Equal(a.Members(), before.Members) {
		t.Fatalf("%+v", d)
	}
	if w.frontier.Revision != before.Revision {
		t.Fatal("adding a member again changed the frontier")
	}
}

// Each refusal names the member it is about and carries the evidence, in the order of the kinds.
func TestFrontierRefusalsNameTheMember(t *testing.T) {
	w := newFrontierWorld(t)
	strong := w.measure(1, []float64{300, 100, 100})
	w.add(strong)

	dominated := w.measure(2, []float64{200, 100, 100})
	d := w.add(dominated)
	refusal, ok := d.(core.FrontierRefusal)
	if !ok || len(refusal.Reasons) != 1 {
		t.Fatalf("%+v, want one refusal", d)
	}
	reason := refusal.Reasons[0]
	if reason.Kind != core.FrontierDominated || reason.Member != strong || reason.Verdict == nil || reason.Verdict.Kind != core.Worse || len(reason.Objectives) != 3 {
		t.Fatalf("%+v", reason)
	}

	same := w.measure(3, []float64{300, 100, 100})
	if kinds := refusalKinds(w.add(same)); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierWithinNoise}) {
		t.Fatalf("%v, want within_noise", kinds)
	}

	other := w.measure(4, []float64{100, 50, 100}, underTask("t2"))
	if kinds := refusalKinds(w.add(other)); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierStale}) {
		t.Fatalf("%v, want stale", kinds)
	}
	elsewhere := w.measure(5, []float64{100, 50, 100}, inEnvironment(dockerEnv))
	if kinds := refusalKinds(w.add(elsewhere)); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierEnvironmentMismatch}) {
		t.Fatalf("%v, want environment_mismatch", kinds)
	}
	unchecked := w.measure(6, []float64{200, 100, 100}, checkedBy("nothing"))
	if kinds := refusalKinds(w.add(unchecked)); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierCheckMissing, core.FrontierDominated}) {
		t.Fatalf("%v, want check_missing then dominated", kinds)
	}
	if kinds := refusalKinds(w.add(commitN(7))); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierUnmeasured}) {
		t.Fatalf("%v, want unmeasured", kinds)
	}
	if got := w.commits(); !slices.Equal(got, []core.Commit{strong}) {
		t.Fatalf("refusals changed the frontier: %v", got)
	}
}

func TestOnlyAParetoMeasurementJoinsAFrontier(t *testing.T) {
	results := []core.Result{measurement("a", candidateCommit, clearWin, 1)}
	_, err := core.DecideFrontier(core.FrontierRequest{Candidate: candidateCommit, Results: results, At: at(2)})
	if failureKind(err) != core.FailureInvalidInput {
		t.Fatalf("a single-metric measurement: %v, want invalid input", err)
	}
	_, err = core.DecideFrontier(core.FrontierRequest{Candidate: candidateCommit, Results: results})
	if failureKind(err) != core.FailureInvalidInput {
		t.Fatalf("no time: %v, want invalid input", err)
	}
}

func TestOnlyADecisionAdmits(t *testing.T) {
	if (core.FrontierAdmission{}).Valid() {
		t.Fatal("the zero admission holds, so a store would record a frontier nobody decided")
	}
	r := rand.New(rand.NewPCG(3, 4))
	w := newFrontierWorld(t)
	for n := range 20 {
		if a, ok := w.add(w.measure(n+1, gridPoint(r))).(core.FrontierAdmission); ok && !a.Valid() {
			t.Fatalf("a decided admission does not hold: %+v", a)
		}
	}
}

// A member's headline numbers are the means of its pooled samples, one per objective, in the task's
// order; a single-metric commit has none.
func TestObjectiveValuesPoolAMembersMeasurements(t *testing.T) {
	results := []core.Result{
		paretoMeasurement("a", candidateCommit, []float64{100, 10, 1}, 1),
		paretoMeasurement("b", candidateCommit, []float64{200, 20, 2}, 2),
		measurement("c", baseCommit, clearWin, 3),
	}
	got := core.ObjectiveValues(candidateCommit, results)
	want := []core.ObjectiveValue{
		{Metric: serving.Metrics[0], Value: 150, Samples: 10},
		{Metric: serving.Metrics[1], Value: 15, Samples: 10},
		{Metric: serving.Metrics[2], Value: 1.5, Samples: 10},
	}
	if !slices.Equal(got, want) {
		t.Fatalf("%+v, want %+v", got, want)
	}
	if got := core.ObjectiveValues(baseCommit, results); got != nil {
		t.Fatalf("a single-metric commit: %+v", got)
	}
}
