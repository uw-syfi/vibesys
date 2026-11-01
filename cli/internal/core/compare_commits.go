package core

import (
	"slices"
	"strings"
)

// CompareRequest asks how one commit's saved measurements compare with another's.
type CompareRequest struct {
	Commit  Commit
	Against Commit
	// Results are saved results. Those of other commits, and the check results, are ignored, so a
	// store may pass everything it has for the two commits.
	Results []Result
	// RelativeNoise is the task's declared relative noise, zero when it declares none.
	RelativeNoise float64
}

// CompareCommits compares a commit's measurements with another commit's. Each side is every
// measurement saved for its commit, pooled into one set of samples: a top-up adds samples to
// what an earlier run took.
//
// Two measurements are pooled, or compared, only when they are of one kind:
//
//   - They were taken in one environment, or the comparison is refused with a *Failure of kind
//     FailureEnvironmentMismatch, and no verdict is given.
//   - They were taken under one task version and score one metric in one direction, or the
//     verdict is inconclusive with ReasonTaskVersionMismatch or ReasonMetricMismatch. The caller
//     decides which measurements to pass; core never guesses a filter.
//
// A side with no measurement fails with FailureNotMeasured. Everything Compare refuses fails the
// same way here.
func CompareCommits(req CompareRequest) (Comparison, error) {
	candidate := measurementsOf(req.Commit, req.Results)
	base := measurementsOf(req.Against, req.Results)
	for _, side := range []struct {
		commit  Commit
		results []Result
	}{{req.Commit, candidate}, {req.Against, base}} {
		if len(side.results) == 0 {
			return Comparison{}, failf(FailureNotMeasured, "commit %s has no measurement", shortCommit(side.commit))
		}
	}
	compared, err := compareMeasurements(candidate, base, req.RelativeNoise)
	if err != nil {
		return Comparison{}, err
	}
	if compared.mixedEnvironments != "" {
		return Comparison{}, failf(FailureEnvironmentMismatch, "%s", compared.mixedEnvironments)
	}
	return Comparison{
		Commit:      req.Commit,
		Against:     req.Against,
		Verdict:     compared.verdict,
		Environment: compared.environment,
	}, nil
}

// measuredComparison is two sides of measurements compared.
type measuredComparison struct {
	// mixedEnvironments is non-empty, and says how, when the measurements were not all taken in
	// one environment. Nothing else is set then.
	mixedEnvironments string
	environment       Fingerprint
	verdict           Verdict
}

// compareMeasurements compares two non-empty sets of measurement results.
func compareMeasurements(candidate, base []Result, relativeNoise float64) (measuredComparison, error) {
	all := slices.Concat(candidate, base)
	environment := all[0].Environment
	if !uniform(all, func(r Result) bool { return r.Environment.Equal(environment) }) {
		return measuredComparison{mixedEnvironments: describeMix(all)}, nil
	}
	candidateSamples, baseSamples := samplesOf(candidate), samplesOf(base)
	result := measuredComparison{environment: environment}
	if reason := mismatch(all); reason != ReasonNone {
		result.verdict = Verdict{
			Kind:             Inconclusive,
			Reason:           reason,
			CandidateSamples: uint32(len(candidateSamples)),
			BaseSamples:      uint32(len(baseSamples)),
		}
		return result, nil
	}
	verdict, err := Compare(CompareInput{
		Direction:     metricOf(all[0]).Direction,
		Candidate:     candidateSamples,
		Base:          baseSamples,
		RelativeNoise: relativeNoise,
	})
	result.verdict = verdict
	return result, err
}

// mismatch says why measurements that were taken in one environment still cannot be pooled: they
// were taken under different task versions, or score different metrics.
func mismatch(results []Result) InconclusiveReason {
	switch {
	case !uniform(results, func(r Result) string { return r.TaskVersion }):
		return ReasonTaskVersionMismatch
	case !uniform(results, metricOf):
		return ReasonMetricMismatch
	}
	return ReasonNone
}

func samplesOf(results []Result) []float64 {
	var out []float64
	for _, r := range results {
		out = append(out, r.Outcome.(MeasureOutcome).Samples...)
	}
	return out
}

// measurementsOf returns the measurements saved for a commit, oldest first.
func measurementsOf(commit Commit, results []Result) []Result {
	var out []Result
	for _, r := range results {
		if _, ok := r.Outcome.(MeasureOutcome); ok && r.Commit == commit {
			out = append(out, r)
		}
	}
	slices.SortFunc(out, func(a, b Result) int {
		if c := a.RecordedAt.Compare(b.RecordedAt); c != 0 {
			return c
		}
		return strings.Compare(string(a.Job), string(b.Job))
	})
	return out
}

func metricOf(r Result) Metric { return r.Outcome.(MeasureOutcome).Metric }

// uniform reports whether every result has the same value of a property. Results is not empty.
func uniform[T comparable](results []Result, property func(Result) T) bool {
	first := property(results[0])
	return !slices.ContainsFunc(results, func(r Result) bool { return property(r) != first })
}

// describeMix names the distinct environments the results were taken in.
func describeMix(results []Result) string {
	var distinct []Fingerprint
	for _, r := range results {
		if !slices.ContainsFunc(distinct, r.Environment.Equal) {
			distinct = append(distinct, r.Environment)
		}
	}
	names := make([]string, len(distinct))
	for i, f := range distinct {
		names[i] = f.String()
	}
	slices.Sort(names)
	return "the measurements were taken in " + strings.Join(names, " and in ") +
		"; compare and promote do not mix environments, so measure both sides in one"
}

func shortCommit(c Commit) string {
	if len(c) > 12 {
		return string(c[:12])
	}
	return string(c)
}
