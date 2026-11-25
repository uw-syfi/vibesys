package core_test

import (
	"math"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A task may declare, for the metric it scores, a noise floor (relative_noise) and a minimum gain
// (min_gain). A minimum gain is a second bar for a better verdict beside the noise test; a floor
// lets a comparison give a verdict from any number of samples, one a side included.

// gainOf is the relative gain a ratio is in a direction.
func gainOf(d core.Direction, ratio float64) float64 {
	if d == core.Minimize {
		return 1 - ratio
	}
	return ratio - 1
}

// A minimum gain changes only a better verdict: one whose gain does not exceed it becomes within
// noise, decided by the minimum gain, and every other verdict and every number stays as it was.
func TestAMinimumGainOnlyHoldsBackABetterVerdict(t *testing.T) {
	r := newRand(60)
	for range properties {
		in := core.CompareInput{Direction: directions()[r.IntN(2)], Candidate: anySamples(r), Base: anySamples(r)}
		if r.IntN(2) == 0 {
			in.RelativeNoise = []float64{0.001, 0.01, 0.05}[r.IntN(3)]
		}
		without, err := core.Compare(in)
		if err != nil {
			continue
		}
		in.MinGain = []float64{0.001, 0.02, 0.1, r.Float64()}[r.IntN(4)]
		with := mustCompare(t, in)

		want := without
		want.MinGain = in.MinGain
		gain := gainOf(in.Direction, without.Ratio)
		if without.Kind == core.Better && !(gain > in.MinGain) {
			want.Kind, want.DecidedBy, want.Threshold = core.WithinNoise, core.BoundMinGain, in.MinGain
		}
		if want.Kind == core.WithinNoise && !(gain > in.MinGain) {
			// No sample count moves the minimum gain, so no count decides a gain below it.
			want.SamplesToDecide = 0
		}
		got := with
		if want.Kind == core.Better && got.DecidedBy == core.BoundMinGain {
			// Either bar may be the higher one; TestTheHigherBarDecidesABetterVerdict says which.
			got.DecidedBy, got.Threshold = core.BoundNoise, got.Critical*got.Noise
		}
		if got != want {
			t.Fatalf("%+v: %+v with a minimum gain, want %+v (without it: %+v)", in, with, want, without)
		}
	}
}

// Under a declared floor the noise bar is two noises, so the bar that decided a better verdict is
// the minimum gain exactly when it is above two noises.
func TestTheHigherBarDecidesABetterVerdict(t *testing.T) {
	r := newRand(61)
	for range properties {
		base := samplesIn(r, 1+r.IntN(5), 100, 0.01)
		in := core.CompareInput{
			Direction:     core.Maximize,
			Candidate:     samplesIn(r, 1+r.IntN(5), 100*(1+r.Float64()*0.1), 0.01),
			Base:          base,
			RelativeNoise: []float64{0.001, 0.005, 0.02}[r.IntN(3)],
			MinGain:       []float64{0.001, 0.01, 0.03, 0.06}[r.IntN(4)],
		}
		v := mustCompare(t, in)
		if v.Kind != core.Better {
			continue
		}
		want := core.BoundNoise
		if in.MinGain > 2*v.Noise {
			want = core.BoundMinGain
		}
		if v.DecidedBy != want {
			t.Fatalf("%+v: better decided by %q with noise %v, want %q", in, v.DecidedBy, v.Noise, want)
		}
	}
}

// With a declared floor, one sample a side gives a verdict, whatever the minimum gain: the noise is
// the floor itself, and the gain is weighed against two noises and the minimum gain.
func TestOneSampleASideUnderADeclaredFloorGivesAVerdict(t *testing.T) {
	r := newRand(62)
	for range properties {
		in := core.CompareInput{
			Direction:     directions()[r.IntN(2)],
			Candidate:     []float64{1 + r.Float64()*1000},
			Base:          []float64{1 + r.Float64()*1000},
			RelativeNoise: 0.0001 + r.Float64()*0.2,
		}
		if r.IntN(2) == 0 {
			in.MinGain = r.Float64() * 0.2
		}
		v := mustCompare(t, in)
		noise := math.Abs(v.Ratio) * in.RelativeNoise * math.Sqrt2
		gain := gainOf(in.Direction, v.Ratio)
		var want core.VerdictKind
		switch {
		case gain > 2*v.Noise && gain > in.MinGain:
			want = core.Better
		case gain < -2*v.Noise:
			want = core.Worse
		default:
			want = core.WithinNoise
		}
		if v.Kind != want || math.Abs(v.Noise-noise) > 1e-12*noise || v.CandidateSamples != 1 || v.BaseSamples != 1 {
			t.Fatalf("%+v: %+v, want %s with noise %v", in, v, want, noise)
		}
	}
}

// The decision rule a campaign froze: one sample a side, a same-code noise of 0.3% declared, and
// promote only at +2% or more. A 4% gain is better, decided by the minimum gain; a 1.5% gain passes
// the noise test but not the minimum gain; a 0.5% gain is within the noise.
func TestACampaignsFrozenDecisionRule(t *testing.T) {
	for _, c := range []struct {
		candidate float64
		kind      core.VerdictKind
		bound     core.VerdictBound
	}{
		{104, core.Better, core.BoundMinGain},
		{101.5, core.WithinNoise, core.BoundMinGain},
		{100.5, core.WithinNoise, core.BoundNoise},
		{96, core.Worse, core.BoundNoise},
	} {
		v := mustCompare(t, core.CompareInput{
			Direction: core.Maximize, Candidate: []float64{c.candidate}, Base: []float64{100}, RelativeNoise: 0.003, MinGain: 0.02,
		})
		if v.Kind != c.kind || v.DecidedBy != c.bound || v.RelativeNoise != 0.003 || v.MinGain != 0.02 {
			t.Errorf("%v against 100: %+v, want %s decided by %s, recording the floor and the minimum gain", c.candidate, v, c.kind, c.bound)
		}
	}
}

// declaring is a single-metric measurement whose task declared a floor and a minimum gain for the
// metric it scores, as the measurement records them.
func declaring(floor, gain float64) func(*core.Result) {
	return func(r *core.Result) {
		out := r.Outcome.(core.MeasureOutcome)
		o := core.Objectives{Metrics: []core.Metric{out.Metric}}
		if floor != 0 {
			o.Noise = []core.ObjectiveNoise{{Name: out.Metric.Name, RelativeNoise: floor}}
		}
		if gain != 0 {
			o.MinGains = []core.ObjectiveMinGain{{Name: out.Metric.Name, MinGain: gain}}
		}
		out.Objectives = o
		r.Outcome = out
	}
}

// compare and promote of a single-metric task judge its measurements under the floor and the
// minimum gain they recorded, so with a floor one sample a side gives a verdict, the same one
// Compare gives, and promote approves exactly a better one.
func TestCompareAndPromoteJudgeOneSampleASideUnderWhatTheMeasurementsRecorded(t *testing.T) {
	r := newRand(63)
	for range properties {
		floor := 0.0001 + r.Float64()*0.05
		gain := []float64{0, 0.01, 0.02, r.Float64() * 0.1}[r.IntN(4)]
		cand, base := []float64{50 + r.Float64()*100}, []float64{50 + r.Float64()*100}
		results := []core.Result{
			measurement("base", baseCommit, base, 1, declaring(floor, gain)),
			measurement("cand", candidateCommit, cand, 2, declaring(floor, gain)),
		}
		want := mustCompare(t, core.CompareInput{Direction: core.Maximize, Candidate: cand, Base: base, RelativeNoise: floor, MinGain: gain})
		if want.Kind == core.Inconclusive {
			t.Fatalf("one sample a side under a floor of %v is inconclusive: %+v", floor, want)
		}
		if got := compareCommits(t, results, 0).Verdict; got != want {
			t.Fatalf("CompareCommits gave %+v, want %+v", got, want)
		}
		req := ready()
		req.Results = results
		switch decision := decide(t, req).(type) {
		case core.Approved:
			if want.Kind != core.Better || decision.Promotion().Verdict != want {
				t.Fatalf("promoted on %+v, want a promotion only on better (%+v)", decision.Promotion().Verdict, want)
			}
		case core.PromoteRefusal:
			if want.Kind == core.Better || len(decision.Reasons) != 1 || *decision.Reasons[0].Verdict != want {
				t.Fatalf("refused with %+v, want a refusal resting on %+v", decision.Reasons, want)
			}
		}
	}
}

// A task that declared nothing still needs three samples a side, so declaring is what turns one
// sample a side into a verdict.
func TestOneSampleASideWithoutADeclaredFloorIsInconclusive(t *testing.T) {
	results := sides([]float64{104}, []float64{100})
	if got := compareCommits(t, results, 0).Verdict; got.Kind != core.Inconclusive || got.Reason != core.ReasonTooFewSamples {
		t.Fatalf("%+v, want inconclusive for too few samples", got)
	}
	declared := []core.Result{
		measurement("base", baseCommit, []float64{100}, 1, declaring(0.003, 0.02)),
		measurement("cand", candidateCommit, []float64{104}, 2, declaring(0.003, 0.02)),
	}
	if got := compareCommits(t, declared, 0).Verdict; got.Kind != core.Better || got.DecidedBy != core.BoundMinGain {
		t.Fatalf("%+v, want better decided by the minimum gain", got)
	}
}
