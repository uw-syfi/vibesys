package core

import (
	"math"
	"slices"
)

// The statistic is the rule Python's vs_core.compare already applies, written again in Go. The
// vectors in testdata/compare_vectors.json were produced by running that code and are checked
// against this one, so the two cannot drift.
//
// Let the candidate have m samples with mean c and the base n samples with mean b. The ratio is
// r = c / b, raw. A run's relative deviation is its distance from its own side's mean over that
// mean. Noise is one standard error of the ratio of two means (first order); a verdict needs the
// gain to exceed a critical number of noises.
//
// A task that declares a noise floor (relative_noise) has stated its noise: the pooled relative
// deviation, never taken below the floor, sets it, and the critical number is declaredNoises.
// A task that declares none has said nothing, so the noise is only what the runs show: Welch's
// t test, with a hard-coded Student-t table for the critical number, and no verdict until each
// side holds MinSamplesWithoutFloor samples that are not all alike.

const (
	// MinSamplesWithoutFloor is how many samples each side needs before a comparison that has no
	// declared noise floor gives a verdict.
	MinSamplesWithoutFloor = 3

	// declaredNoises is how many standard errors a gain must exceed for a verdict under a
	// declared noise floor.
	declaredNoises = 2.0

	// degreesSlack is added before flooring Welch's degrees of freedom. Degrees that are a whole
	// number in exact arithmetic can come out a few units in the last place below it in floating
	// point, and the floor would then drop a whole degree.
	degreesSlack = 1e-9

	// firstTDegrees is the degrees of freedom of the first table entry.
	firstTDegrees = 2
)

// studentT975 is the two-sided 95% Student-t quantile for firstTDegrees (2) to 30 degrees of
// freedom, the standard table to three decimals. Above 30 the last entry is used, slightly above
// the normal 1.96. The table is hard-coded: nothing here computes a quantile.
var studentT975 = [...]float64{
	4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
	2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
	2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
}

// CompareInput is two sets of samples of one metric.
type CompareInput struct {
	Direction Direction
	Candidate []float64
	Base      []float64
	// RelativeNoise is the task's declared relative variation of one run. Zero means the task
	// declares none and Welch's t test decides.
	RelativeNoise float64
}

// Compare compares a candidate's samples with a base's. It is a pure function of its input: the
// same samples, in any order, always give the same Verdict.
//
// It fails with a *Failure when there is nothing to compare, and never answers inconclusive for
// those: FailureNotMeasured for an empty side, and FailureInvalidInput for a direction that is
// neither maximize nor minimize, a negative or non-finite RelativeNoise, a sample that is not
// finite, a base mean of zero (a ratio over it means nothing), or samples so extreme that a
// number derived from them overflows.
//
// Nothing the verdict rests on is rounded. Ratio is raw and not oriented by the direction.
func Compare(in CompareInput) (Verdict, error) {
	if err := validateCompare(in); err != nil {
		return Verdict{}, err
	}
	// Sorted copies make the sums independent of the order the samples arrived in.
	candidate, base := sorted(in.Candidate), sorted(in.Base)
	c, b := mean(candidate), mean(base)
	if !finite(c) || !finite(b) {
		return Verdict{}, failf(FailureInvalidInput, "the sample means overflow")
	}
	if b == 0 {
		return Verdict{}, failf(FailureInvalidInput, "the base mean is zero, so a ratio over it means nothing")
	}
	ratio := c / b
	m, n := len(candidate), len(base)

	var noise, critical, degrees float64
	known := true
	if in.RelativeNoise > 0 {
		sigma := math.Max(pooledSpread(candidate, c, base, b), in.RelativeNoise)
		noise = math.Abs(ratio) * sigma * math.Sqrt(1/float64(m)+1/float64(n))
		critical = declaredNoises
	} else {
		noise, degrees = welch(candidate, c, base, b)
		noise *= math.Abs(ratio)
		critical = studentCritical(degrees)
		known = min(m, n) >= MinSamplesWithoutFloor && noise > 0
	}
	if !finite(ratio) || !finite(noise) || math.IsNaN(degrees) {
		return Verdict{}, failf(FailureInvalidInput, "the samples are too extreme to compare: the ratio or its noise overflows")
	}

	verdict := Verdict{
		CandidateSamples: uint32(m),
		BaseSamples:      uint32(n),
		CandidateMean:    c,
		BaseMean:         b,
		Ratio:            ratio,
		Noise:            noise,
	}
	gain := ratio - 1
	if in.Direction == Minimize {
		gain = 1 - ratio
	}
	switch {
	case !known && min(m, n) < MinSamplesWithoutFloor:
		verdict.Kind, verdict.Reason = Inconclusive, ReasonTooFewSamples
	case !known:
		verdict.Kind, verdict.Reason = Inconclusive, ReasonConstantSamples
	case gain > critical*noise:
		verdict.Kind = Better
	case gain < -critical*noise:
		verdict.Kind = Worse
	default:
		verdict.Kind = WithinNoise
	}
	return verdict, nil
}

func validateCompare(in CompareInput) error {
	if in.Direction != Maximize && in.Direction != Minimize {
		return failf(FailureInvalidInput, "direction %q is neither %q nor %q", in.Direction, Maximize, Minimize)
	}
	if !finite(in.RelativeNoise) || in.RelativeNoise < 0 {
		return failf(FailureInvalidInput, "relative noise %v is not a finite number of at least zero", in.RelativeNoise)
	}
	for _, side := range []struct {
		name    string
		samples []float64
	}{{"candidate", in.Candidate}, {"base", in.Base}} {
		if len(side.samples) == 0 {
			return failf(FailureNotMeasured, "the %s has no samples", side.name)
		}
		for i, sample := range side.samples {
			if !finite(sample) {
				return failf(FailureInvalidInput, "%s sample %d is not finite (%v)", side.name, i, sample)
			}
		}
	}
	return nil
}

// pooledSpread is the pooled relative deviation of the runs from their own side's mean. It is
// zero when the two sides hold two samples or fewer between them, or a mean is zero. It is used
// under a declared floor only, which bounds it from below.
func pooledSpread(candidate []float64, c float64, base []float64, b float64) float64 {
	degrees := len(candidate) + len(base) - 2
	if degrees <= 0 || c == 0 || b == 0 {
		return 0
	}
	return math.Sqrt((squares(candidate, c) + squares(base, b)) / float64(degrees))
}

// welch returns the relative standard error of the ratio of two means and its
// Welch-Satterthwaite degrees of freedom. Each side contributes its own relative variance over its
// count; a side with one run, identical runs or a zero mean contributes none. No error means no
// degrees.
func welch(candidate []float64, c float64, base []float64, b float64) (relativeError, degrees float64) {
	partC := variance(candidate, c) / float64(len(candidate))
	partB := variance(base, b) / float64(len(base))
	total := partC + partB
	if total == 0 {
		return 0, 0
	}
	var spread float64
	for _, part := range []struct {
		value float64
		free  int
	}{{partC, len(candidate) - 1}, {partB, len(base) - 1}} {
		if part.value > 0 {
			spread += part.value * part.value / float64(part.free)
		}
	}
	return math.Sqrt(total), total * total / spread
}

// variance is one side's relative variance: squared relative deviations over its runs less one.
func variance(xs []float64, mean float64) float64 {
	if len(xs) == 1 || mean == 0 {
		return 0
	}
	return squares(xs, mean) / float64(len(xs)-1)
}

// squares is the sum of squared relative deviations from the mean, and exactly zero for identical
// runs: the float mean of identical values can differ from them in the last bit, which would show
// a spread of about 1e-17 where the runs showed none.
func squares(xs []float64, mean float64) float64 {
	if slices.Min(xs) == slices.Max(xs) {
		return 0
	}
	var total float64
	for _, x := range xs {
		deviation := (x - mean) / mean
		total += deviation * deviation
	}
	return total
}

// studentCritical is the two-sided 95% Student-t quantile at the floor of the degrees of freedom.
// The floor, because the quantile falls as the degrees grow, so rounding down never gives a
// verdict the exact degrees would not. Below two degrees there is no verdict, so the bar is
// infinite.
func studentCritical(degrees float64) float64 {
	whole := math.Floor(degrees + degreesSlack)
	if !(whole >= firstTDegrees) { // also true for NaN
		return math.Inf(1)
	}
	return studentT975[int(min(whole-firstTDegrees, float64(len(studentT975)-1)))]
}

func mean(xs []float64) float64 {
	var total float64
	for _, x := range xs {
		total += x
	}
	return total / float64(len(xs))
}

func sorted(xs []float64) []float64 {
	out := slices.Clone(xs)
	slices.Sort(out)
	return out
}

func finite(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }
