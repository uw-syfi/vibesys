package setup_test

import (
	"math"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

func nanValue() float64 { return math.NaN() }
func posInf() float64   { return math.Inf(1) }

func state(r setup.Result, id core.SetupCheckID) setup.State {
	for _, c := range r.Checks {
		if c.ID == id {
			return c.State
		}
	}
	panic("no check " + string(id))
}

func eachSeed(t *testing.T, body func(t *testing.T, seed uint64)) {
	t.Helper()
	for seed := range uint64(cases) {
		body(t, seed)
		if t.Failed() {
			t.Fatalf("seed %d", seed)
		}
	}
}

func TestDeriveIsTotalAndReportsSixChecksInOrder(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		var f setup.Facts
		if seed%3 != 0 {
			f = garbage(r)
		}
		for _, p := range []setup.Policy{setup.Standard, {}, {MinSamples: -3, DefaultNoise: nanValue()}} {
			res := setup.Derive(f, p)
			var unmet []core.SetupCheckID
			for i, c := range res.Checks {
				if c.ID != setup.Order[i] {
					t.Fatalf("check %d is %s", i, c.ID)
				}
				if c.State != setup.Passed {
					unmet = append(unmet, c.ID)
				}
			}
			if !slices.Equal(res.Unmet(), unmet) {
				t.Fatalf("Unmet %v, want %v", res.Unmet(), unmet)
			}
		}
	})
}

func TestDeriveIgnoresTheOrderOfItsInputs(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		f := genFacts(r)
		want := setup.Derive(f, setup.Standard)
		g := f
		g.Jobs, g.Measurements = slices.Clone(f.Jobs), slices.Clone(f.Measurements)
		r.Shuffle(len(g.Jobs), func(i, j int) { g.Jobs[i], g.Jobs[j] = g.Jobs[j], g.Jobs[i] })
		r.Shuffle(len(g.Measurements), func(i, j int) { g.Measurements[i], g.Measurements[j] = g.Measurements[j], g.Measurements[i] })
		if got := setup.Derive(g, setup.Standard); !reflect.DeepEqual(got, want) {
			t.Fatalf("shuffled facts give %+v, want %+v", got, want)
		}
	})
}

func TestJobsOfAnotherTaskVersionNeverChangeTheResult(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		f := genFacts(r)
		want := setup.Derive(f, setup.Standard)
		g := f
		g.Jobs = slices.Clone(f.Jobs)
		for i := range 1 + r.IntN(8) {
			g.Jobs = append(g.Jobs, genJob(r, 100+i, pick(r, "v0", "v2", "")))
		}
		if got := setup.Derive(g, setup.Standard); !reflect.DeepEqual(got, want) {
			t.Fatalf("stale jobs changed %+v to %+v", want, got)
		}
	})
}

func TestOnlyTheBasePassesOrFailsBaseAndStability(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		f := genFacts(r)
		want := setup.Derive(f, setup.Standard)
		g := f
		g.Jobs = slices.Clone(f.Jobs)
		for i := range 1 + r.IntN(8) {
			j := genJob(r, 100+i, version)
			if j.Relation == setup.RelationBase {
				j.Relation, j.Commit = setup.RelationDescendant, "m"
			}
			g.Jobs = append(g.Jobs, j)
		}
		got := setup.Derive(g, setup.Standard)
		for _, id := range []core.SetupCheckID{core.CheckBasePasses, core.CheckNumberStable} {
			if state(got, id) != state(want, id) {
				t.Fatalf("a job off the base moved %s from %v to %v", id, state(want, id), state(got, id))
			}
		}
	})
}

func TestAncestorUnrelatedAndUnknownCommitsNeverProveBrokenFails(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		f := genFacts(r)
		var kept []setup.Job
		for _, j := range f.Jobs {
			if j.Relation != setup.RelationDescendant {
				kept = append(kept, j)
			}
		}
		for i := range 6 {
			j := genJob(r, 100+i, version)
			j.Kind, j.State, j.CodeChanged = setup.KindCheck, setup.JobFailed, true
			j.ExitCode, j.FailedStage = ptr(int32(1)), setup.StageCheck
			j.Relation, j.Commit = pick(r, setup.RelationAncestor, setup.RelationUnrelated, setup.RelationUnknown), "elsewhere"
			kept = append(kept, j)
		}
		f.Jobs = kept
		if got := state(setup.Derive(f, setup.Standard), core.CheckBrokenFails); got == setup.Passed {
			t.Fatalf("BROKEN_FAILS passed on %+v", f.Jobs)
		}
	})
}

func TestAFailureOutsideTheCheckStageIsNeverARejection(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		f := genFacts(r)
		for i := range f.Jobs {
			if f.Jobs[i].FailedStage == setup.StageCheck {
				f.Jobs[i].FailedStage = pick(r, setup.StageBuild, setup.StageMeasure, setup.StageUnrecorded)
			}
			if f.Jobs[i].State == setup.JobFailed {
				f.Jobs[i].ExitCode = ptr(int32(1 + r.IntN(100)))
			}
		}
		res := setup.Derive(f, setup.Standard)
		if state(res, core.CheckBasePasses) == setup.Failed {
			t.Fatalf("a failure outside the check stage rejected the base: %+v", res.Checks[3])
		}
		if state(res, core.CheckBrokenFails) == setup.Passed {
			t.Fatalf("a failure outside the check stage broke the mutant: %+v", res.Checks[4])
		}
	})
}

// witness is a second, naive reading of the table for the three checks that read jobs and series.
type witness struct{ base, broken, stable bool }

func spreadOf(xs []float64) float64 {
	mean := 0.0
	for _, x := range xs {
		mean += x
	}
	mean /= float64(len(xs))
	ss := 0.0
	for _, x := range xs {
		ss += (x - mean) * (x - mean)
	}
	return math.Sqrt(ss/float64(len(xs)-1)) / math.Abs(mean)
}

func witnesses(f setup.Facts, p setup.Policy) witness {
	var w witness
	rejected := func(j setup.Job) bool {
		return j.State == setup.JobFailed && j.ExitCode != nil && *j.ExitCode != 0 && j.FailedStage == setup.StageCheck
	}
	var newest *setup.Job
	for i, j := range f.Jobs {
		if j.Version == f.Version && j.Kind == setup.KindCheck && j.Relation == setup.RelationBase && (j.State == setup.JobSucceeded || rejected(j)) {
			if newest == nil || j.FinishedAt.After(newest.FinishedAt) || (j.FinishedAt.Equal(newest.FinishedAt) && j.ID > newest.ID) {
				newest = &f.Jobs[i]
			}
		}
	}
	w.base = newest != nil && newest.State == setup.JobSucceeded
	for _, j := range f.Jobs {
		if w.base && j.Version == f.Version && j.Kind == setup.KindCheck && j.Relation == setup.RelationDescendant && j.CodeChanged && rejected(j) {
			w.broken = true
		}
	}
	var best *setup.Measurement
	for i, m := range f.Measurements {
		if m.Commit == f.Base.Commit && m.Version == f.Version && (best == nil || len(m.Samples) > len(best.Samples)) {
			best = &f.Measurements[i]
		}
	}
	if best == nil || len(f.Decl.Objectives) == 0 {
		return w
	}
	w.stable = true
	for _, o := range f.Decl.Objectives {
		xs := best.Samples
		if o.Name != best.Metric {
			xs = nil
			for _, s := range best.Observations {
				if s.Name == o.Name {
					xs = s.Values
				}
			}
		}
		floor := p.DefaultNoise
		if o.NoiseFloor != nil {
			floor = *o.NoiseFloor
		}
		if len(xs) < p.MinSamples || spreadOf(xs) > floor || mean(xs) <= 0 {
			w.stable = false
		}
	}
	return w
}

func mean(xs []float64) float64 {
	s := 0.0
	for _, x := range xs {
		s += x
	}
	return s / float64(len(xs))
}

func TestPassedMeansAWitnessExistsAndEveryWitnessPasses(t *testing.T) {
	passed := map[string]int{}
	eachSeed(t, func(t *testing.T, seed uint64) {
		f := genFacts(newRand(seed))
		res := setup.Derive(f, setup.Standard)
		w := witnesses(f, setup.Standard)
		for _, c := range []struct {
			id  core.SetupCheckID
			win bool
		}{{core.CheckBasePasses, w.base}, {core.CheckBrokenFails, w.broken}, {core.CheckNumberStable, w.stable}} {
			if got := state(res, c.id) == setup.Passed; got != c.win {
				t.Fatalf("%s passed=%v, witness=%v\nfacts %+v\nchecks %+v", c.id, got, c.win, f, res.Checks)
			}
			if c.win {
				passed[string(c.id)]++
			}
		}
	})
	for _, id := range []core.SetupCheckID{core.CheckBasePasses, core.CheckBrokenFails, core.CheckNumberStable} {
		if passed[string(id)] < 5 {
			t.Fatalf("the generator reaches %s passing only %d times", id, passed[string(id)])
		}
	}
}

func TestBrokenFailsNeverPassesWithoutBasePasses(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		res := setup.Derive(garbage(newRand(seed)), setup.Standard)
		if state(res, core.CheckBrokenFails) == setup.Passed && state(res, core.CheckBasePasses) != setup.Passed {
			t.Fatalf("BROKEN_FAILS passed over %+v", res.Checks[3])
		}
	})
}

func TestALaterPassingJobNeverUnPassesTheChecksItProves(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		f := genFacts(r)
		res := setup.Derive(f, setup.Standard)
		last := epoch.Add(24 * time.Hour)
		j := genJob(r, 500, version)
		j.Kind, j.State, j.FinishedAt, j.ExitCode, j.FailedStage = setup.KindCheck, setup.JobSucceeded, last, nil, setup.StageUnrecorded
		j.Relation, j.Commit, j.CodeChanged = setup.RelationBase, base, false
		g := f
		g.Jobs = append(slices.Clone(f.Jobs), j)
		if got := state(setup.Derive(g, setup.Standard), core.CheckBasePasses); got != setup.Passed {
			t.Fatalf("a later passing check left BASE_PASSES at %v", got)
		}
		if state(res, core.CheckBrokenFails) == setup.Passed {
			m := j
			m.ID, m.Relation, m.Commit, m.CodeChanged = "j501", setup.RelationDescendant, "mutant", true
			g.Jobs = append(g.Jobs, m)
			if got := state(setup.Derive(g, setup.Standard), core.CheckBrokenFails); got != setup.Passed {
				t.Fatalf("a later accepted mutant un-passed BROKEN_FAILS: %v", got)
			}
		}
	})
}

func stableFacts(xs []float64, floor *float64) setup.Facts {
	return setup.Facts{
		Version: version, Base: setup.Base{Commit: base, Version: version},
		Decl:         setup.Declarations{Objectives: []setup.Objective{{Name: "o0", NoiseFloor: floor}}},
		Measurements: []setup.Measurement{{Commit: base, Version: version, Metric: "o0", Value: mean(xs), Samples: xs}},
	}
}

func TestNumberStableFollowsTheSpreadAgainstTheFloor(t *testing.T) {
	eachSeed(t, func(t *testing.T, seed uint64) {
		r := newRand(seed)
		n := 5 + r.IntN(6)
		xs := make([]float64, n)
		for i := range xs {
			xs[i] = float64(1 + r.IntN(100)) // exact sums, so the spread below is the one Derive computes
		}
		spread := spreadOf(xs)
		if spread >= 1 {
			return
		}
		at := func(floor float64, ys []float64) setup.State {
			return state(setup.Derive(stableFacts(ys, &floor), setup.Standard), core.CheckNumberStable)
		}
		if got := at(spread, xs); got != setup.Passed {
			t.Fatalf("spread equal to the floor: %v", got)
		}
		if spread > 0 {
			if got := at(math.Nextafter(spread, 0), xs); got != setup.Failed {
				t.Fatalf("spread just above the floor: %v", got)
			}
		}
		scaled := make([]float64, n)
		perm := r.Perm(n)
		c := pick(r, 0.25, 0.5, 2.0, 8.0, 1024.0)
		for i, k := range perm {
			scaled[i] = xs[k] * c
		}
		if got := at(spread*1.0000001, scaled); got != setup.Passed {
			t.Fatalf("scaled and permuted samples at the floor: %v", got)
		}
		if got := at(spread, xs[:r.IntN(setup.Standard.MinSamples)]); got == setup.Passed {
			t.Fatalf("passed under MinSamples")
		}
	})
}

func TestNumberStableUsesTheDefaultFloorAndSaysSo(t *testing.T) {
	steady := []float64{100, 101, 99, 100, 100}
	res := setup.Derive(stableFacts(steady, nil), setup.Standard)
	if c := res.Checks[5]; c.State != setup.Passed {
		t.Fatalf("%+v", c)
	}
	noisy := []float64{100, 120, 80, 100, 100}
	c := setup.Derive(stableFacts(noisy, nil), setup.Standard).Checks[5]
	if c.State != setup.Failed || !contains(c.Detail, "default floor") || !contains(c.Detail, "5.0%") {
		t.Fatalf("%+v", c)
	}
	own := 0.5
	if c := setup.Derive(stableFacts(noisy, &own), setup.Standard).Checks[5]; c.State != setup.Passed {
		t.Fatalf("declared floor: %+v", c)
	}
	for _, xs := range [][]float64{{0, 0, 0, 0, 0}, {-1, -2, -1, -2, -1}} {
		if c := setup.Derive(stableFacts(xs, &own), setup.Standard).Checks[5]; c.State != setup.Failed || !contains(c.Detail, "not positive") {
			t.Fatalf("%v: %+v", xs, c)
		}
	}
}

func contains(s, sub string) bool { return strings.Contains(s, sub) }

func TestUnreadableIsAllPendingWithTheReadersWords(t *testing.T) {
	res := setup.Unreadable("manifest half written")
	for i, c := range res.Checks {
		if c.ID != setup.Order[i] || c.State != setup.Pending || c.Detail != "manifest half written" {
			t.Fatalf("%+v", c)
		}
	}
	if res.Baseline != nil || len(res.Unmet()) != 6 {
		t.Fatalf("%+v", res)
	}
}

func TestEachCellOfTheTable(t *testing.T) {
	exit1 := ptr(int32(1))
	check := func(id string, rel setup.Relation, commit string, st setup.JobState, at int) setup.Job {
		return setup.Job{ID: id, Kind: setup.KindCheck, State: st, Relation: rel, Commit: commit, Version: version, FinishedAt: epoch.Add(time.Duration(at) * time.Second), CodeChanged: rel == setup.RelationDescendant}
	}
	rej := func(j setup.Job, stage setup.Stage) setup.Job {
		j.State, j.ExitCode, j.FailedStage = setup.JobFailed, exit1, stage
		return j
	}
	facts := func(jobs ...setup.Job) setup.Facts {
		f := stableFacts(nil, nil)
		f.Measurements = nil
		f.Jobs = jobs
		return f
	}
	at := func(f setup.Facts, i int) setup.Check { return setup.Derive(f, setup.Standard).Checks[i] }

	if c := at(facts(), 3); c.State != setup.Pending || c.Detail != "no check of the base yet" {
		t.Fatalf("%+v", c)
	}
	late := facts()
	late.Base.Version = "v0"
	if c := at(late, 3); c.State != setup.Pending || c.Detail != "commit the task" {
		t.Fatalf("%+v", c)
	}
	ok := check("a", setup.RelationBase, base, setup.JobSucceeded, 1)
	rows := []struct {
		name  string
		facts setup.Facts
		idx   int
		want  setup.State
	}{
		{"base passes", facts(ok), 3, setup.Passed},
		{"base queued", facts(check("a", setup.RelationBase, base, setup.JobQueued, 0)), 3, setup.Running},
		{"base rejected", facts(rej(check("a", setup.RelationBase, base, 0, 1), setup.StageCheck)), 3, setup.Failed},
		{"base build failure", facts(rej(check("a", setup.RelationBase, base, 0, 1), setup.StageBuild)), 3, setup.Pending},
		{"base lost", facts(check("a", setup.RelationBase, base, setup.JobLost, 1)), 3, setup.Pending},
		{"base passes after a rejection", facts(rej(check("a", setup.RelationBase, base, 0, 1), setup.StageCheck), check("b", setup.RelationBase, base, setup.JobSucceeded, 2)), 3, setup.Passed},
		{"broken needs the base", facts(rej(check("m", setup.RelationDescendant, "m1", 0, 1), setup.StageCheck)), 4, setup.Pending},
		{"broken passes", facts(ok, rej(check("m", setup.RelationDescendant, "m1", 0, 2), setup.StageCheck)), 4, setup.Passed},
		{"mutant build failure", facts(ok, rej(check("m", setup.RelationDescendant, "m1", 0, 2), setup.StageBuild)), 4, setup.Pending},
		{"mutant without a code change", facts(ok, func() setup.Job {
			j := rej(check("m", setup.RelationDescendant, "m1", 0, 2), setup.StageCheck)
			j.CodeChanged = false
			return j
		}()), 4, setup.Pending},
		{"mutant running", facts(ok, check("m", setup.RelationDescendant, "m1", setup.JobRunning, 0)), 4, setup.Running},
		{"mutant accepted", facts(ok, check("m", setup.RelationDescendant, "m1", setup.JobSucceeded, 2)), 4, setup.Failed},
	}
	for _, c := range rows {
		if got := at(c.facts, c.idx).State; got != c.want {
			t.Errorf("%s: %v, want %v", c.name, got, c.want)
		}
	}
	if c := at(facts(ok, check("m", setup.RelationDescendant, "m1", setup.JobSucceeded, 2)), 4); !contains(c.Detail, "m1") {
		t.Fatalf("the hole is not named: %+v", c)
	}
	f := facts(ok, rej(check("m", setup.RelationDescendant, "m1", 0, 2), setup.StageCheck))
	f.Jobs[1].Version = "v0"
	if c := at(f, 4); c.State != setup.Pending {
		t.Fatalf("a mutant under another version counted: %+v", c)
	}
}
