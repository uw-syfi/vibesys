package core_test

import (
	"fmt"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var (
	candidateCommit = commitN(2)
	baseCommit      = commitN(1)
)

// sides saves the samples of each side as one measurement per side.
func sides(candidate, base []float64) []core.Result {
	return []core.Result{
		measurement("base", baseCommit, base, 1),
		measurement("cand", candidateCommit, candidate, 2),
	}
}

func compareCommits(t testing.TB, results []core.Result, relativeNoise float64) core.Comparison {
	t.Helper()
	got, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: results, RelativeNoise: relativeNoise})
	if err != nil {
		t.Fatalf("CompareCommits: %v", err)
	}
	return got
}

func TestCompareCommitsAgreesWithCompare(t *testing.T) {
	r := newRand(40)
	for range properties {
		cand, base := anySamples(r), anySamples(r)
		floor := 0.0
		if r.IntN(3) == 0 {
			floor = r.Float64() * 0.1
		}
		for _, direction := range directions() {
			results := sides(cand, base)
			for i := range results {
				results[i] = withMetric(results[i], core.Metric{Name: "m", Direction: direction})
			}
			want, wantErr := core.Compare(core.CompareInput{Direction: direction, Candidate: cand, Base: base, RelativeNoise: floor})
			got, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: results, RelativeNoise: floor})
			if failureKind(err) != failureKind(wantErr) {
				t.Fatalf("error %v, want %v", err, wantErr)
			}
			if err != nil {
				continue
			}
			if got.Verdict != want || got.Commit != candidateCommit || got.Against != baseCommit || !got.Environment.Equal(hostEnv) {
				t.Fatalf("%+v, want verdict %+v", got, want)
			}
		}
	}
}

func withMetric(r core.Result, m core.Metric) core.Result { scoring(m)(&r); return r }

// Measurements of a commit pool: samples that arrive in several runs, in any order, compare as
// one set, and other commits and check results are ignored.
func TestMeasurementsOfACommitPool(t *testing.T) {
	r := newRand(41)
	for range properties {
		cand, base := samplesIn(r, 3+r.IntN(8), 120, 0.05), samplesIn(r, 3+r.IntN(8), 100, 0.05)
		want := compareCommits(t, sides(cand, base), 0)

		var results []core.Result
		split := func(commit core.Commit, prefix string, samples []float64) {
			for i := 0; len(samples) > 0; i++ {
				n := 1 + r.IntN(len(samples))
				results = append(results, measurement(fmt.Sprintf("%s%d", prefix, i), commit, samples[:n], r.IntN(50)))
				samples = samples[n:]
			}
		}
		split(candidateCommit, "c", cand)
		split(baseCommit, "b", base)
		results = append(results, // noise: another commit's measurements and the checks
			measurement("o", commitN(9), samplesIn(r, 5, 1, 0.5), 3),
			checkPass("ck1", candidateCommit, 4),
			checkPass("ck2", baseCommit, 5))
		r.Shuffle(len(results), func(i, j int) { results[i], results[j] = results[j], results[i] })

		got := compareCommits(t, results, 0)
		if got.Verdict.CandidateSamples != uint32(len(cand)) || got.Verdict.BaseSamples != uint32(len(base)) {
			t.Fatalf("samples %d and %d, want %d and %d", got.Verdict.CandidateSamples, got.Verdict.BaseSamples, len(cand), len(base))
		}
		if got.Verdict.Kind != want.Verdict.Kind || got.Verdict.Ratio != want.Verdict.Ratio || got.Verdict.Noise != want.Verdict.Noise {
			t.Fatalf("pooled verdict %+v, want %+v", got.Verdict, want.Verdict)
		}
	}
}

func TestACommitWithoutMeasurementsIsNotMeasured(t *testing.T) {
	good := sides([]float64{1, 2, 3}, []float64{1, 2, 3})
	for name, results := range map[string][]core.Result{
		"nothing saved":        nil,
		"no candidate":         good[:1],
		"no base":              good[1:],
		"only checks":          {checkPass("a", candidateCommit, 1), checkPass("b", baseCommit, 1)},
		"only other commits":   {measurement("o", commitN(9), []float64{1, 2, 3}, 1)},
		"checks and one side":  append(good[:1:1], checkPass("a", candidateCommit, 1)),
		"results of commit+1s": {measurement("x", commitN(3), []float64{1, 2, 3}, 1), good[0]},
	} {
		_, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: results})
		if failureKind(err) != core.FailureNotMeasured {
			t.Errorf("%s: %v, want a not_measured failure", name, err)
		}
	}
}

func TestMixedEnvironmentsAreRefused(t *testing.T) {
	otherDigest := core.Fingerprint{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: fmt.Sprintf("sha256:%064x", 2)}
	otherFacts := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux", "arch": "arm64"}}
	otherBackend := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCUDA, Facts: hostEnv.Facts}
	for name, other := range map[string]core.Fingerprint{
		"host and docker":      dockerEnv,
		"two images":           otherDigest,
		"one fact differs":     otherFacts,
		"another backend":      otherBackend,
		"a missing fact table": {Kind: core.EnvironmentHost, Backend: core.BackendCPU},
	} {
		results := sides([]float64{120, 121, 119}, []float64{100, 101, 99})
		results[1].Environment = other
		got, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: results})
		if failureKind(err) != core.FailureEnvironmentMismatch || got.Commit != "" || got.Verdict != (core.Verdict{}) {
			t.Errorf("%s: %+v, %v, want an environment_mismatch failure and no comparison", name, got, err)
		}
	}

	// A mix inside one side counts too.
	results := append(sides([]float64{120, 121, 119}, []float64{100, 101, 99}),
		measurement("cand2", candidateCommit, []float64{120, 119, 121}, 3, inEnvironment(dockerEnv)))
	if _, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: results}); failureKind(err) != core.FailureEnvironmentMismatch {
		t.Errorf("a side taken in two environments: %v, want an environment_mismatch failure", err)
	}
}

// Fingerprints that are equal as values, whatever their fact tables' identity, are one
// environment.
func TestEqualEnvironmentsAreOneEnvironment(t *testing.T) {
	results := sides([]float64{120, 121, 119}, []float64{100, 101, 99})
	copied := core.Fingerprint{Kind: hostEnv.Kind, Backend: hostEnv.Backend, Facts: map[string]string{"arch": "amd64", "os": "linux"}}
	results[1].Environment = copied
	got := compareCommits(t, results, 0)
	if got.Verdict.Kind != core.Better || !got.Environment.Equal(hostEnv) {
		t.Fatalf("%+v, want better in the host environment", got)
	}
}

func TestMeasurementsOfDifferentKindsAreInconclusive(t *testing.T) {
	cases := []struct {
		name   string
		change func(*core.Result)
		reason core.InconclusiveReason
	}{
		{"task version", underTask("t2"), core.ReasonTaskVersionMismatch},
		{"metric name", scoring(core.Metric{Name: "latency", Direction: core.Maximize}), core.ReasonMetricMismatch},
		{"metric direction", scoring(core.Metric{Name: "throughput", Direction: core.Minimize}), core.ReasonMetricMismatch},
	}
	for _, c := range cases {
		results := sides([]float64{120, 121, 119, 122}, []float64{100, 101, 99})
		c.change(&results[1])
		got := compareCommits(t, results, 0)
		v := got.Verdict
		if v.Kind != core.Inconclusive || v.Reason != c.reason || v.CandidateSamples != 4 || v.BaseSamples != 3 {
			t.Errorf("%s: %+v, want inconclusive (%s) with the counts of both sides", c.name, v, c.reason)
		}
		if v.Ratio != 0 || v.Noise != 0 || v.CandidateMean != 0 || v.BaseMean != 0 {
			t.Errorf("%s: %+v, want no numbers where nothing was compared", c.name, v)
		}
	}
	// The task version outranks the metric when both differ.
	results := sides([]float64{120, 121, 119}, []float64{100, 101, 99})
	underTask("t2")(&results[1])
	scoring(core.Metric{Name: "latency", Direction: core.Minimize})(&results[1])
	if got := compareCommits(t, results, 0); got.Verdict.Reason != core.ReasonTaskVersionMismatch {
		t.Errorf("both differ: %+v, want the task version mismatch", got.Verdict)
	}
}

// What a store hands over in any order gives the same answer, and the answer does not read the
// slice it was given.
func TestCompareCommitsDoesNotDependOnOrderAndLeavesInputAlone(t *testing.T) {
	r := newRand(42)
	for range properties {
		results := sides(samplesIn(r, 3+r.IntN(5), 120, 0.1), samplesIn(r, 3+r.IntN(5), 100, 0.1))
		results = append(results, measurement("c2", candidateCommit, samplesIn(r, 2, 120, 0.1), 3))
		want := compareCommits(t, results, 0)
		before := slices.Clone(results)
		shuffled := slices.Clone(results)
		r.Shuffle(len(shuffled), func(i, j int) { shuffled[i], shuffled[j] = shuffled[j], shuffled[i] })
		if got := compareCommits(t, shuffled, 0); got.Verdict.Kind != want.Verdict.Kind || got.Verdict.Ratio != want.Verdict.Ratio {
			t.Fatalf("shuffled: %+v, want %+v", got.Verdict, want.Verdict)
		}
		for i := range results {
			if results[i].Job != before[i].Job {
				t.Fatal("CompareCommits reordered the caller's results")
			}
		}
	}
}
