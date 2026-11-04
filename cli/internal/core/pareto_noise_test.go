package core_test

import (
	"math/rand/v2"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// An objective may declare a noise floor of its own (Objectives.Noise), overriding the task's
// [pareto] default for that objective.

// noisyMeasurement is a measurement of a task with the given objectives and one series of samples
// per objective, in the order of o.Metrics; the first is the scored metric.
func noisyMeasurement(job string, commit core.Commit, o core.Objectives, series [][]float64, minute int) core.Result {
	return measurement(job, commit, series[0], minute, func(r *core.Result) {
		out := r.Outcome.(core.MeasureOutcome)
		out.Metric = o.Metrics[0]
		out.Objectives = o
		for i, m := range o.Metrics[1:] {
			out.Observations = append(out.Observations, core.Observation{Name: m.Name, Values: series[i+1]})
		}
		r.Outcome = out
	})
}

// twoObjectives are throughput (maximized, scored) and p99 latency (minimized).
func twoObjectives(defaultNoise float64, own ...core.ObjectiveNoise) core.Objectives {
	return core.Objectives{
		Metrics: []core.Metric{
			{Name: "throughput", Direction: core.Maximize},
			{Name: "p99", Direction: core.Minimize},
		},
		RelativeNoise: defaultNoise,
		Noise:         own,
	}
}

// randomSeries draws one series per objective whose levels differ between the sides by up to
// +-25%, with a spread of up to 20% a run.
func randomSeries(r *rand.Rand, n int) (cand, base [][]float64) {
	for range 2 {
		level := 50 + r.Float64()*100
		spread := r.Float64() * 0.2
		base = append(base, samplesIn(r, n, level, spread))
		cand = append(cand, samplesIn(r, n, level*(0.75+r.Float64()*0.5), spread))
	}
	return cand, base
}

func compareObjectivesOf(t testing.TB, o core.Objectives, cand, base [][]float64) core.Comparison {
	return compareCommits(t, []core.Result{
		noisyMeasurement("base", baseCommit, o, base, 1),
		noisyMeasurement("cand", candidateCommit, o, cand, 2),
	}, 0)
}

// Property (a): an objective that declares nothing is compared as before, under the [pareto]
// floor, and declaring the default value explicitly changes no verdict.
func TestObjectivesWithoutTheirOwnFloorUseTheParetoFloor(t *testing.T) {
	r := newRand(201)
	for range properties {
		def := []float64{0, 0.005, 0.03, 0.1}[r.IntN(4)]
		cand, base := randomSeries(r, 3+r.IntN(5))
		plain := twoObjectives(def)
		got := compareObjectivesOf(t, plain, cand, base)
		for i, m := range plain.Metrics {
			want := mustCompare(t, core.CompareInput{Direction: m.Direction, Candidate: cand[i], Base: base[i], RelativeNoise: def})
			if got.Objectives[i].Verdict != want {
				t.Fatalf("floor %v, objective %s: %+v, want %+v", def, m.Name, got.Objectives[i].Verdict, want)
			}
		}
		explicit := twoObjectives(def, core.ObjectiveNoise{Name: "throughput", RelativeNoise: def}, core.ObjectiveNoise{Name: "p99", RelativeNoise: def})
		again := compareObjectivesOf(t, explicit, cand, base)
		if again.Verdict != got.Verdict || again.Objectives[0] != got.Objectives[0] || again.Objectives[1] != got.Objectives[1] {
			t.Fatalf("declaring the default floor changed the comparison: %+v, was %+v", again, got)
		}
	}
}

// Property (b): each objective's verdict is the one Compare gives under that objective's own
// floor, whatever the other's is.
func TestEachObjectiveIsJudgedUnderItsOwnFloor(t *testing.T) {
	r := newRand(202)
	differs := 0
	for range properties {
		def := r.Float64() * 0.1
		own := []core.ObjectiveNoise{}
		floors := []float64{def, def}
		for i, m := range []string{"throughput", "p99"} {
			if r.IntN(3) > 0 {
				floors[i] = r.Float64() * 0.2
				own = append(own, core.ObjectiveNoise{Name: m, RelativeNoise: floors[i]})
			}
		}
		o := twoObjectives(def, own...)
		cand, base := randomSeries(r, 3+r.IntN(5))
		got := compareObjectivesOf(t, o, cand, base)
		for i, m := range o.Metrics {
			want := mustCompare(t, core.CompareInput{Direction: m.Direction, Candidate: cand[i], Base: base[i], RelativeNoise: floors[i]})
			if got.Objectives[i].Verdict != want {
				t.Fatalf("objective %s under floor %v: %+v, want %+v", m.Name, floors[i], got.Objectives[i].Verdict, want)
			}
			if def != floors[i] && want.Kind != mustCompare(t, core.CompareInput{Direction: m.Direction, Candidate: cand[i], Base: base[i], RelativeNoise: def}).Kind {
				differs++
			}
		}
	}
	if differs == 0 {
		t.Fatal("no generated case had a per-objective floor change a verdict, so the property proves nothing")
	}
}

// The motivating case: a 5% throughput gain is better under a 3% throughput floor, while a p99
// that moved 5% is within noise under its 15% floor, so the commit is better overall. Under the
// one 15% floor for both, the throughput gain is hidden.
func TestAThroughputGainSurvivesItsFloorWhileP99NoiseDoesNot(t *testing.T) {
	five := func(level float64) []float64 { return []float64{level, level, level, level, level} }
	base := [][]float64{five(100), five(200)}
	cand := [][]float64{five(105), five(190)} // throughput +5%, p99 -5% (better for a minimize)

	split := twoObjectives(0.15, core.ObjectiveNoise{Name: "throughput", RelativeNoise: 0.03})
	got := compareObjectivesOf(t, split, cand, base)
	if k := got.Objectives[0].Verdict.Kind; k != core.Better {
		t.Errorf("throughput under a 3%% floor: %s, want better", k)
	}
	if k := got.Objectives[1].Verdict.Kind; k != core.WithinNoise {
		t.Errorf("p99 under a 15%% floor: %s, want within_noise", k)
	}
	if got.Verdict.Kind != core.Better {
		t.Errorf("overall %s, want better", got.Verdict.Kind)
	}
	if one := compareObjectivesOf(t, twoObjectives(0.15), cand, base); one.Verdict.Kind != core.WithinNoise {
		t.Errorf("under one 15%% floor the overall verdict is %s, want within_noise", one.Verdict.Kind)
	}
}

// Measurements judged under different floors are different objectives: they are not pooled.
func TestObjectivesWithDifferentFloorsAreNotEqual(t *testing.T) {
	a := twoObjectives(0.05)
	b := twoObjectives(0.05, core.ObjectiveNoise{Name: "p99", RelativeNoise: 0.15})
	c := twoObjectives(0.05, core.ObjectiveNoise{Name: "p99", RelativeNoise: 0.2})
	if a.Equal(b) || b.Equal(c) || !b.Equal(twoObjectives(0.05, core.ObjectiveNoise{Name: "p99", RelativeNoise: 0.15})) {
		t.Fatal("Equal does not tell objectives with different floors apart")
	}
}
