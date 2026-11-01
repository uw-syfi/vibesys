package core_test

import (
	"encoding/binary"
	"math"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestCompareRefusesWhatHasNoComparison(t *testing.T) {
	good := []float64{1, 2, 3}
	cases := []struct {
		name  string
		in    core.CompareInput
		kind  core.FailureKind
		names string // text the message must carry, so the caller can see which input was wrong
	}{
		{"empty candidate", core.CompareInput{Direction: core.Maximize, Base: good}, core.FailureNotMeasured, "candidate"},
		{"empty base", core.CompareInput{Direction: core.Maximize, Candidate: good}, core.FailureNotMeasured, "base"},
		{"candidate not finite", core.CompareInput{Direction: core.Maximize, Candidate: []float64{1, math.NaN()}, Base: good}, core.FailureInvalidInput, "candidate sample 1"},
		{"base infinite", core.CompareInput{Direction: core.Maximize, Candidate: good, Base: []float64{math.Inf(-1)}}, core.FailureInvalidInput, "base sample 0"},
		{"base mean zero", core.CompareInput{Direction: core.Maximize, Candidate: good, Base: []float64{0, 0, 0}}, core.FailureInvalidInput, "base mean is zero"},
		{"base mean zero from signed samples", core.CompareInput{Direction: core.Minimize, Candidate: good, Base: []float64{-1, 0, 1}}, core.FailureInvalidInput, "base mean is zero"},
		{"base mean zero under a floor", core.CompareInput{Direction: core.Maximize, Candidate: good, Base: []float64{0}, RelativeNoise: 0.05}, core.FailureInvalidInput, "base mean is zero"},
		{"direction missing", core.CompareInput{Candidate: good, Base: good}, core.FailureInvalidInput, "direction"},
		{"direction unknown", core.CompareInput{Direction: "max", Candidate: good, Base: good}, core.FailureInvalidInput, "direction"},
		{"negative floor", core.CompareInput{Direction: core.Maximize, Candidate: good, Base: good, RelativeNoise: -0.1}, core.FailureInvalidInput, "relative noise"},
		{"NaN floor", core.CompareInput{Direction: core.Maximize, Candidate: good, Base: good, RelativeNoise: math.NaN()}, core.FailureInvalidInput, "relative noise"},
		{"means overflow", core.CompareInput{Direction: core.Maximize, Candidate: []float64{math.MaxFloat64, math.MaxFloat64}, Base: good}, core.FailureInvalidInput, "overflow"},
		{"ratio overflows", core.CompareInput{Direction: core.Maximize, Candidate: []float64{1e308}, Base: []float64{1e-308}}, core.FailureInvalidInput, "too extreme"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			verdict, err := core.Compare(c.in)
			if got := failureKind(err); got != c.kind {
				t.Fatalf("failure kind %q (error %v, verdict %+v), want %q", got, err, verdict, c.kind)
			}
			if !strings.Contains(err.Error(), c.names) {
				t.Errorf("error %q does not name %q", err, c.names)
			}
			if verdict != (core.Verdict{}) {
				t.Errorf("a refused comparison returned a verdict: %+v", verdict)
			}
		})
	}
}

// Welch's degrees of freedom are a whole number in exact arithmetic when the two sides have the
// same relative spread, and float arithmetic can land a few units in the last place below it.
// Without the slack that is added before flooring, the floor drops a degree and the bar moves from
// 2.776 to 3.182. This case sits at 2.96 noises, between the two bars, and has degrees 4 less
// about 1e-15.
func TestDegreesSlackKeepsWholeDegrees(t *testing.T) {
	verdict := mustCompare(t, core.CompareInput{
		Direction: core.Maximize,
		Candidate: []float64{18.798, 16.518, 16.9968},
		Base:      []float64{15.665, 13.765, 14.164},
	})
	if verdict.Kind != core.Better {
		t.Fatalf("verdict %s, want better: the degrees of freedom must floor to 4, not 3", verdict.Kind)
	}
}

// Above 30 degrees of freedom the table's last entry, 2.042, is the bar. Forty runs a side have
// about 78 degrees; this case sits at 2.0435 noises, between 2.042 and the 2.045 of the entry
// before it, so a table that stopped short, or a bar read off the normal curve, would call it
// within noise.
func TestStudentTableIsClampedAtItsLastEntry(t *testing.T) {
	base := make([]float64, 40)
	candidate := make([]float64, 40)
	for i := range base {
		base[i] = float64(50 + i%7)
		candidate[i] = 1.017494 * base[i]
	}
	verdict := mustCompare(t, core.CompareInput{Direction: core.Maximize, Candidate: candidate, Base: base})
	if verdict.Kind != core.Better {
		t.Fatalf("verdict %s at %.4f noises, want better against the bar 2.042", verdict.Kind, (verdict.Ratio-1)/verdict.Noise)
	}
}

func TestCompareHasNoRoundingInTheDecision(t *testing.T) {
	// A ratio that prints as 1.1000 at four places is not 1.1.
	verdict := mustCompare(t, core.CompareInput{
		Direction: core.Maximize,
		Candidate: []float64{11.000001, 11.000002, 11.000003},
		Base:      []float64{10, 10.5, 9.5},
	})
	if verdict.Ratio == 1.1 || verdict.Ratio != verdict.CandidateMean/verdict.BaseMean {
		t.Errorf("ratio %v was rounded or oriented: want exactly candidate mean over base mean", verdict.Ratio)
	}
}

// comparisons draws random comparisons over the contract's whole input space, and gives each to a
// test with its verdict when there is one.
func comparisons(t *testing.T, seed uint64, check func(in core.CompareInput, verdict core.Verdict, err error)) {
	t.Helper()
	r := newRand(seed)
	for range properties {
		in := core.CompareInput{
			Direction: directions()[r.IntN(2)],
			Candidate: anySamples(r),
			Base:      anySamples(r),
		}
		if r.IntN(3) == 0 {
			in.RelativeNoise = []float64{0.001, 0.01, 0.05, 0.2, 1}[r.IntN(5)]
		}
		verdict, err := core.Compare(in)
		check(in, verdict, err)
	}
}

func TestCompareAnswersOrRefusesWithATypedFailure(t *testing.T) {
	comparisons(t, 1, func(in core.CompareInput, verdict core.Verdict, err error) {
		if err != nil {
			if failureKind(err) == "" || verdict != (core.Verdict{}) {
				t.Fatalf("%+v: error %v is not a Failure alone", in, err)
			}
			return
		}
		checkVerdictShape(t, in, verdict)
	})
}

func checkVerdictShape(t testing.TB, in core.CompareInput, v core.Verdict) {
	t.Helper()
	if v.CandidateSamples != uint32(len(in.Candidate)) || v.BaseSamples != uint32(len(in.Base)) {
		t.Fatalf("%+v: counts %d and %d", in, v.CandidateSamples, v.BaseSamples)
	}
	if v.Ratio != v.CandidateMean/v.BaseMean {
		t.Fatalf("%+v: ratio %v is not the raw candidate mean over the base mean", in, v.Ratio)
	}
	for _, x := range []float64{v.CandidateMean, v.BaseMean, v.Ratio, v.Noise} {
		if math.IsNaN(x) || math.IsInf(x, 0) {
			t.Fatalf("%+v: a number in the verdict is not finite: %+v", in, v)
		}
	}
	if v.BaseMean == 0 || v.Noise < 0 {
		t.Fatalf("%+v: base mean %v, noise %v", in, v.BaseMean, v.Noise)
	}
	if (v.Kind == core.Inconclusive) != (v.Reason != core.ReasonNone) {
		t.Fatalf("%+v: kind %s with reason %q", in, v.Kind, v.Reason)
	}
	switch v.Kind {
	case core.Better:
		if (in.Direction == core.Maximize) != (v.Ratio > 1) {
			t.Fatalf("%+v: better, but the ratio %v is on the wrong side of 1", in, v.Ratio)
		}
	case core.Worse:
		if (in.Direction == core.Maximize) != (v.Ratio < 1) {
			t.Fatalf("%+v: worse, but the ratio %v is on the wrong side of 1", in, v.Ratio)
		}
	}
}

func TestCompareDoesNotDependOnSampleOrder(t *testing.T) {
	r := newRand(2)
	comparisons(t, 2, func(in core.CompareInput, verdict core.Verdict, err error) {
		shuffled := in
		shuffled.Candidate = slices.Clone(in.Candidate)
		shuffled.Base = slices.Clone(in.Base)
		r.Shuffle(len(shuffled.Candidate), func(i, j int) {
			shuffled.Candidate[i], shuffled.Candidate[j] = shuffled.Candidate[j], shuffled.Candidate[i]
		})
		r.Shuffle(len(shuffled.Base), func(i, j int) { shuffled.Base[i], shuffled.Base[j] = shuffled.Base[j], shuffled.Base[i] })
		again, againErr := core.Compare(shuffled)
		if again != verdict || failureKind(againErr) != failureKind(err) {
			t.Fatalf("%+v: shuffled to %+v/%+v: %+v (%v), want %+v (%v)", in, shuffled.Candidate, shuffled.Base, again, againErr, verdict, err)
		}
	})
}

func TestCompareIsInvariantUnderPowerOfTwoScaling(t *testing.T) {
	// Scaling by a power of two is exact in floating point, so the whole verdict scales with it:
	// the unit a metric is reported in cannot change a verdict.
	r := newRand(3)
	comparisons(t, 3, func(in core.CompareInput, verdict core.Verdict, err error) {
		if err != nil {
			return
		}
		scale := math.Ldexp(1, r.IntN(41)-20)
		scaled := in
		scaled.Candidate = scaledBy(in.Candidate, scale)
		scaled.Base = scaledBy(in.Base, scale)
		got := mustCompare(t, scaled)
		want := verdict
		want.CandidateMean *= scale
		want.BaseMean *= scale
		if got != want {
			t.Fatalf("%+v scaled by %v: %+v, want %+v", in, scale, got, want)
		}
	})
}

func scaledBy(xs []float64, scale float64) []float64 {
	out := make([]float64, len(xs))
	for i, x := range xs {
		out[i] = x * scale
	}
	return out
}

func TestCompareDirectionsMirrorEachOther(t *testing.T) {
	mirror := map[core.VerdictKind]core.VerdictKind{
		core.Better: core.Worse, core.Worse: core.Better,
		core.WithinNoise: core.WithinNoise, core.Inconclusive: core.Inconclusive,
	}
	comparisons(t, 4, func(in core.CompareInput, verdict core.Verdict, err error) {
		if err != nil {
			return
		}
		flipped := in
		flipped.Direction = opposite(in.Direction)
		got := mustCompare(t, flipped)
		want := verdict
		want.Kind = mirror[verdict.Kind]
		if got != want {
			t.Fatalf("%+v: flipping the direction gave %+v, want %+v", in, got, want)
		}
	})
}

func TestCompareWithoutAFloorNeedsThreeSamplesASide(t *testing.T) {
	comparisons(t, 5, func(in core.CompareInput, verdict core.Verdict, err error) {
		if err != nil || in.RelativeNoise != 0 {
			return
		}
		few := min(len(in.Candidate), len(in.Base)) < core.MinSamplesWithoutFloor
		switch {
		case few && (verdict.Kind != core.Inconclusive || verdict.Reason != core.ReasonTooFewSamples):
			t.Fatalf("%+v: %s (%s) from fewer than %d samples a side", in, verdict.Kind, verdict.Reason, core.MinSamplesWithoutFloor)
		case !few && verdict.Reason == core.ReasonTooFewSamples:
			t.Fatalf("%+v: too few samples reported for %d and %d", in, len(in.Candidate), len(in.Base))
		}
	})
}

func TestCompareWithoutAFloorIsNeverConclusiveOnConstantSides(t *testing.T) {
	// A deterministic benchmark reads the same again: identical runs are no estimate of noise,
	// whatever the gain.
	r := newRand(6)
	for range properties {
		in := core.CompareInput{
			Direction: directions()[r.IntN(2)],
			Candidate: constant(3+r.IntN(5), 1+r.Float64()*100),
			Base:      constant(3+r.IntN(5), 1+r.Float64()*100),
		}
		verdict := mustCompare(t, in)
		if verdict.Kind != core.Inconclusive || verdict.Reason != core.ReasonConstantSamples {
			t.Fatalf("%+v: %s (%s), want inconclusive for constant sides", in, verdict.Kind, verdict.Reason)
		}
	}
}

func constant(n int, v float64) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = v
	}
	return out
}

func TestCompareWithAFloorAlwaysGivesAVerdict(t *testing.T) {
	comparisons(t, 7, func(in core.CompareInput, verdict core.Verdict, err error) {
		if err == nil && in.RelativeNoise > 0 && verdict.Kind == core.Inconclusive {
			t.Fatalf("%+v: inconclusive under a declared floor", in)
		}
	})
}

func TestRaisingTheFloorCannotCreateAVerdictOfDifference(t *testing.T) {
	// A larger declared noise widens the bar: it can turn better into within noise, never the
	// reverse.
	r := newRand(8)
	for range properties {
		in := core.CompareInput{Direction: directions()[r.IntN(2)], Candidate: anySamples(r), Base: anySamples(r)}
		low := 0.001 + r.Float64()*0.1
		in.RelativeNoise = low
		atLow, err := core.Compare(in)
		if err != nil {
			continue
		}
		in.RelativeNoise = low * (1 + r.Float64()*10)
		atHigh := mustCompare(t, in)
		if atHigh.Kind != core.WithinNoise && atHigh.Kind != atLow.Kind {
			t.Fatalf("%+v: %s at floor %v, but %s at the lower floor", in, atHigh.Kind, in.RelativeNoise, atLow.Kind)
		}
	}
}

// samplesFromBytes reads 8 bytes at a time as float64 bit patterns, so a fuzzer reaches NaN,
// infinities, zeros and denormals.
func samplesFromBytes(data []byte) []float64 {
	var out []float64
	for ; len(data) >= 8; data = data[8:] {
		out = append(out, math.Float64frombits(binary.LittleEndian.Uint64(data)))
	}
	return out
}

func FuzzCompare(f *testing.F) {
	seed := func(candidate, base []float64) []byte {
		var data []byte
		for _, x := range append(slices.Clone(candidate), base...) {
			data = binary.LittleEndian.AppendUint64(data, math.Float64bits(x))
		}
		return data
	}
	f.Add(seed([]float64{20, 21, 22}, []float64{10, 10.5, 9.5}), uint8(3), uint8(0), true)
	f.Add(seed([]float64{1, 2, 3}, []float64{0, 0, 0}), uint8(3), uint8(0), true)
	f.Add(seed([]float64{120}, []float64{100}), uint8(1), uint8(2), false)
	f.Add(seed([]float64{math.NaN(), 1}, []float64{1, 2, 3}), uint8(2), uint8(0), true)
	f.Add(seed([]float64{math.MaxFloat64, math.MaxFloat64}, []float64{1e-300}), uint8(2), uint8(1), false)
	f.Add(seed([]float64{5e-324, 1e-320, 1e-310}, []float64{1e-300, 2e-300, 3e-300}), uint8(3), uint8(0), true)
	f.Fuzz(func(t *testing.T, data []byte, split uint8, floor uint8, maximize bool) {
		all := samplesFromBytes(data)
		cut := min(int(split), len(all))
		in := core.CompareInput{
			Direction:     core.Minimize,
			Candidate:     all[:cut],
			Base:          all[cut:],
			RelativeNoise: []float64{0, 0.01, 0.05, 0.5}[floor%4],
		}
		if maximize {
			in.Direction = core.Maximize
		}
		verdict, err := core.Compare(in)
		if err != nil {
			if failureKind(err) == "" || verdict != (core.Verdict{}) {
				t.Fatalf("%+v: error %v is not a Failure alone", in, err)
			}
			return
		}
		checkVerdictShape(t, in, verdict)
		reversed := core.CompareInput{
			Direction:     in.Direction,
			Candidate:     slices.Clone(in.Candidate),
			Base:          slices.Clone(in.Base),
			RelativeNoise: in.RelativeNoise,
		}
		slices.Reverse(reversed.Candidate)
		slices.Reverse(reversed.Base)
		if again := mustCompare(t, reversed); again != verdict {
			t.Fatalf("%+v: reversing the samples changed %+v to %+v", in, verdict, again)
		}
	})
}
