package core_test

import (
	"math"
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A verdict says the bar it was weighed against (Critical, Threshold), what set its noise
// (SigmaSource, ObservedSpread) and, for a gain within noise, how many samples a side would decide
// it (SamplesToDecide). Saying so changes no verdict.

// The bar a verdict records is the one its kind was decided against: a better gain is above it, a
// worse one below its negative, and a gain within noise is neither, or is above the noise bar and
// not above the minimum gain that decided it.
func TestTheThresholdIsTheBarTheVerdictWasDecidedAgainst(t *testing.T) {
	for seed := uint64(70); seed < 74; seed++ {
		comparisons(t, seed, func(in core.CompareInput, v core.Verdict, err error) {
			if err != nil {
				return
			}
			checkBar(t, in, v)
		})
	}
}

func checkBar(t *testing.T, in core.CompareInput, v core.Verdict) {
	t.Helper()
	gain := gainOf(in.Direction, v.Ratio)
	noiseBar := v.Critical * v.Noise
	if v.Kind == core.Inconclusive {
		if v.Critical != 0 || v.Threshold != 0 || v.SamplesToDecide != 0 {
			t.Fatalf("%+v: an inconclusive verdict records a bar: %+v", in, v)
		}
		return
	}
	wantCritical := declaredCritical(in)
	if in.RelativeNoise == 0 && !slices.Contains(studentT975[:], v.Critical) {
		t.Fatalf("%+v: critical %v is no Student-t quantile", in, v.Critical)
	}
	if in.RelativeNoise != 0 && v.Critical != wantCritical {
		t.Fatalf("%+v: critical %v under a declared floor, want %v", in, v.Critical, wantCritical)
	}
	switch v.DecidedBy {
	case core.BoundNoise:
		if v.Threshold != noiseBar {
			t.Fatalf("%+v: decided by the noise test, but the threshold %v is not %v × %v", in, v.Threshold, v.Critical, v.Noise)
		}
	case core.BoundMinGain:
		if v.Threshold != in.MinGain {
			t.Fatalf("%+v: decided by the minimum gain, but the threshold is %v", in, v.Threshold)
		}
	default:
		t.Fatalf("%+v: a %s verdict decided by %q", in, v.Kind, v.DecidedBy)
	}
	var holds bool
	switch {
	case v.Kind == core.Better:
		holds = gain > v.Threshold && gain > noiseBar && gain > in.MinGain
	case v.Kind == core.Worse:
		holds = gain < -v.Threshold
	case v.DecidedBy == core.BoundMinGain:
		holds = gain > noiseBar && !(gain > v.Threshold)
	default:
		holds = !(gain > v.Threshold) && !(gain < -v.Threshold)
	}
	if !holds {
		t.Fatalf("%+v: a gain of %v is not %s against the threshold %v (noise bar %v)", in, gain, v.Kind, v.Threshold, noiseBar)
	}
}

// declaredCritical is how many noises a gain must exceed under a declared floor.
func declaredCritical(in core.CompareInput) float64 {
	if in.RelativeNoise == 0 {
		return 0
	}
	return 2
}

// The noise rests on the declared floor when the floor is at least the spread the runs show, and
// on that spread otherwise; a task that declares no floor has only the spread.
func TestTheSigmaSourceSaysWhatSetTheNoise(t *testing.T) {
	comparisons(t, 75, func(in core.CompareInput, v core.Verdict, err error) {
		if err != nil {
			return
		}
		want := core.SigmaObservedSpread
		if in.RelativeNoise != 0 && in.RelativeNoise >= v.ObservedSpread {
			want = core.SigmaDeclaredFloor
		}
		if v.SigmaSource != want {
			t.Fatalf("%+v: sigma from %q with an observed spread of %v, want %q", in, v.SigmaSource, v.ObservedSpread, want)
		}
		if in.RelativeNoise == 0 {
			return
		}
		sigma := max(v.ObservedSpread, in.RelativeNoise)
		m, n := float64(v.CandidateSamples), float64(v.BaseSamples)
		if noise := math.Abs(v.Ratio) * sigma * math.Sqrt(1/m+1/n); noise != v.Noise {
			t.Fatalf("%+v: noise %v, but sigma %v from %q gives %v", in, v.Noise, sigma, v.SigmaSource, noise)
		}
	})
}

// Saying the bar changes no verdict: for any input, the kind, reason, bound and numbers are those
// of the rule as it stood before verdicts carried their bar, which verdictBeforeTheBar restates.
func TestCarryingTheBarChangesNoVerdict(t *testing.T) {
	for seed := uint64(76); seed < 80; seed++ {
		comparisons(t, seed, func(in core.CompareInput, v core.Verdict, err error) {
			if err != nil {
				return
			}
			want := verdictBeforeTheBar(in)
			got := v
			got.Critical, got.Threshold, got.SigmaSource, got.ObservedSpread, got.SamplesToDecide = 0, 0, "", 0, 0
			if got != want {
				t.Fatalf("%+v: %+v, but the rule before verdicts carried their bar gives %+v", in, got, want)
			}
		})
	}
}

// studentT975 is the table the rule uses, restated for the oracle.
var studentT975 = [...]float64{
	4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
	2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
	2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
}

// verdictBeforeTheBar is Compare as it was before verdicts carried their bar, for an input it
// accepts: an oracle written from that rule, so a change to how the verdict is reached shows here.
func verdictBeforeTheBar(in core.CompareInput) core.Verdict {
	candidate, base := sortedCopy(in.Candidate), sortedCopy(in.Base)
	c, b := core.Headline(candidate), core.Headline(base)
	ratio := c / b
	m, n := len(candidate), len(base)
	var noise, critical float64
	known := true
	if in.RelativeNoise > 0 {
		pooled := 0.0
		if free := m + n - 2; free > 0 && c != 0 {
			pooled = math.Sqrt((squaresOf(candidate, c) + squaresOf(base, b)) / float64(free))
		}
		sigma := math.Max(pooled, in.RelativeNoise)
		noise = math.Abs(ratio) * sigma * math.Sqrt(1/float64(m)+1/float64(n))
		critical = 2
	} else {
		partC, partB := varianceOf(candidate, c)/float64(m), varianceOf(base, b)/float64(n)
		total, degrees := partC+partB, 0.0
		if total > 0 {
			var spread float64
			if partC > 0 {
				spread += partC * partC / float64(m-1)
			}
			if partB > 0 {
				spread += partB * partB / float64(n-1)
			}
			noise, degrees = math.Sqrt(total), total*total/spread
		}
		noise *= math.Abs(ratio)
		critical = math.Inf(1)
		if whole := math.Floor(degrees + 1e-9); whole >= 2 {
			critical = studentT975[int(min(whole-2, float64(len(studentT975)-1)))]
		}
		known = min(m, n) >= core.MinSamplesWithoutFloor && noise > 0
	}
	v := core.Verdict{
		CandidateSamples: uint32(m), BaseSamples: uint32(n), CandidateMean: c, BaseMean: b, Ratio: ratio,
		Noise: noise, RelativeNoise: in.RelativeNoise, MinGain: in.MinGain,
	}
	gain := gainOf(in.Direction, ratio)
	switch {
	case !known && min(m, n) < core.MinSamplesWithoutFloor:
		v.Kind, v.Reason = core.Inconclusive, core.ReasonTooFewSamples
	case !known:
		v.Kind, v.Reason = core.Inconclusive, core.ReasonConstantSamples
	case gain > critical*noise && gain > in.MinGain:
		v.Kind, v.DecidedBy = core.Better, core.BoundNoise
		if in.MinGain > critical*noise {
			v.DecidedBy = core.BoundMinGain
		}
	case gain > critical*noise:
		v.Kind, v.DecidedBy = core.WithinNoise, core.BoundMinGain
	case gain < -critical*noise:
		v.Kind, v.DecidedBy = core.Worse, core.BoundNoise
	default:
		v.Kind, v.DecidedBy = core.WithinNoise, core.BoundNoise
	}
	return v
}

func sortedCopy(xs []float64) []float64 {
	out := slices.Clone(xs)
	slices.Sort(out)
	return out
}

func squaresOf(xs []float64, mean float64) float64 {
	if slices.Min(xs) == slices.Max(xs) {
		return 0
	}
	var total float64
	for _, x := range xs {
		d := (x - mean) / mean
		total += d * d
	}
	return total
}

func varianceOf(xs []float64, mean float64) float64 {
	if len(xs) == 1 || mean == 0 {
		return 0
	}
	return squaresOf(xs, mean) / float64(len(xs)-1)
}

// SamplesToDecide is set exactly for a gain within noise that is positive and above the minimum
// gain, and it is the fewest samples a side that decide it: k samples a side with the same means
// and the same sigma read better, and k-1 do not.
func TestSamplesToDecideIsTheFewestThatClearTheBar(t *testing.T) {
	r := newRand(81)
	checked := 0
	for seed := uint64(82); seed < 90; seed++ {
		comparisons(t, seed, func(in core.CompareInput, v core.Verdict, err error) {
			if err != nil {
				return
			}
			gain := gainOf(in.Direction, v.Ratio)
			reachable := v.Kind == core.WithinNoise && gain > 0 && gain > in.MinGain
			if !reachable {
				if v.SamplesToDecide != 0 {
					t.Fatalf("%+v: %s with a gain of %v says %d samples a side would decide it", in, v.Kind, gain, v.SamplesToDecide)
				}
				return
			}
			k := int(v.SamplesToDecide)
			if k == 0 || k > 2000 {
				return // too many samples to build; the count is weighed by the same rule either way
			}
			at := func(k int) (core.Verdict, bool) { return compareAtSamples(t, r, in, v, k) }
			decided, ok := at(k)
			if ok && decided.Kind != core.Better {
				t.Fatalf("%+v: %d samples a side would decide it, but they give %+v", in, k, decided)
			}
			if fewest := fewestThatShowTheSigma(in); k-1 >= fewest {
				if short, ok := at(k - 1); ok && short.Kind == core.Better {
					t.Fatalf("%+v: %d samples a side would decide it, but %d already do: %+v", in, k, k-1, short)
				}
			}
			checked++
		})
	}
	if checked < 50 {
		t.Fatalf("only %d comparisons had a count of samples to check", checked)
	}
}

// fewestThatShowTheSigma is the fewest samples a side whose comparison weighs the same sigma: three
// without a floor, where fewer give no verdict, and two when the observed spread sets it, since
// one sample a side shows no spread.
func fewestThatShowTheSigma(in core.CompareInput) int {
	if in.RelativeNoise == 0 {
		return core.MinSamplesWithoutFloor
	}
	return 2
}

// compareAtSamples compares k samples a side with the means of the verdict's sides and the spread
// its noise rests on: each side's own relative variance without a floor, the pooled spread when it
// set the noise, and none under a floor that did. Building means and spreads rounds them, so it
// reports false when the gain sits too close to the bar for that rounding to be ignored.
func compareAtSamples(t *testing.T, r *rand.Rand, in core.CompareInput, v core.Verdict, k int) (core.Verdict, bool) {
	t.Helper()
	varC, varB := 0.0, 0.0
	switch {
	case in.RelativeNoise == 0:
		varC = varianceOf(sortedCopy(in.Candidate), v.CandidateMean)
		varB = varianceOf(sortedCopy(in.Base), v.BaseMean)
	case v.SigmaSource == core.SigmaObservedSpread:
		varC, varB = v.ObservedSpread*v.ObservedSpread, v.ObservedSpread*v.ObservedSpread
	}
	at := in
	at.Candidate, at.Base = sideOf(r, k, v.CandidateMean, varC), sideOf(r, k, v.BaseMean, varB)
	got := mustCompare(t, at)
	gain := gainOf(in.Direction, got.Ratio)
	bar := got.Critical * got.Noise
	return got, math.Abs(gain-bar) > 1e-6*math.Abs(bar)
}

// sideOf is k samples, in any order, whose mean is mean and whose relative variance (squared
// relative deviations over k-1) is variance: half at mean·(1+d) and half at mean·(1-d), with one
// more at the mean when k is odd.
func sideOf(r *rand.Rand, k int, mean, variance float64) []float64 {
	d := math.Sqrt(variance)
	if k%2 == 0 {
		d = math.Sqrt(variance * float64(k-1) / float64(k))
	}
	out := make([]float64, 0, k)
	for range k / 2 {
		out = append(out, mean*(1+d), mean*(1-d))
	}
	if k%2 == 1 {
		out = append(out, mean)
	}
	r.Shuffle(len(out), func(i, j int) { out[i], out[j] = out[j], out[i] })
	return out
}

// The case that asked for the bar: two samples a side, a gain of about 6.8%, and a provisional
// floor of 5% that set a bar of about 10.7% while the runs varied by under 1%. The verdict is
// within noise, as it was, and now says that the floor set the bar, where it stood, and that six
// samples a side with the same means would clear it.
func TestTwoAgainstTwoUnderAProvisionalFloorSaysItsBar(t *testing.T) {
	in := core.CompareInput{
		Direction:     core.Maximize,
		Candidate:     []float64{28.39, 28.27},
		Base:          []float64{26.51, 26.57},
		RelativeNoise: 0.05,
	}
	v := mustCompare(t, in)
	if v.Kind != core.WithinNoise || v.DecidedBy != core.BoundNoise {
		t.Fatalf("%+v, want within noise decided by the noise test", v)
	}
	if v.SigmaSource != core.SigmaDeclaredFloor || !(v.ObservedSpread < 0.01) {
		t.Fatalf("sigma from %q with an observed spread of %v, want the declared floor over a spread under 1%%", v.SigmaSource, v.ObservedSpread)
	}
	if v.Critical != 2 || math.Abs(v.Threshold-0.1067) > 0.0005 || v.Threshold != 2*v.Noise {
		t.Fatalf("critical %v, threshold %v, noise %v: want a bar of 2 noises, about 10.67%%", v.Critical, v.Threshold, v.Noise)
	}
	// At k samples a side the bar is 2·ratio·0.05·sqrt(2/k): about 6.75% at five, just above the
	// gain of 6.74%, and 6.16% at six.
	if v.SamplesToDecide != 6 {
		t.Fatalf("%d samples a side would decide it, want 6", v.SamplesToDecide)
	}
	five := in
	five.Candidate, five.Base = []float64{28.39, 28.27, 28.39, 28.27, 28.33}, []float64{26.51, 26.57, 26.51, 26.57, 26.54}
	if got := mustCompare(t, five); got.Kind != core.WithinNoise {
		t.Fatalf("five samples a side: %+v, want still within noise", got)
	}
	six := in
	six.Candidate, six.Base = []float64{28.39, 28.27, 28.39, 28.27, 28.39, 28.27}, []float64{26.51, 26.57, 26.51, 26.57, 26.51, 26.57}
	if got := mustCompare(t, six); got.Kind != core.Better {
		t.Fatalf("six samples a side: %+v, want better", got)
	}
}
