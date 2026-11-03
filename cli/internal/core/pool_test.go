package core_test

import (
	"fmt"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A commit's samples split over any number of measure jobs pool into one measurement holding
// every sample in the jobs' order, whose headline is the one compare uses, with checks and other
// commits left out.
func TestMeasureJobsOfACommitPoolIntoOneMeasurement(t *testing.T) {
	r := newRand(77)
	for range properties {
		samples := samplesIn(r, 2+r.IntN(10), 120, 0.05)
		var results []core.Result
		var jobs []core.JobID
		rest := samples
		for i := 0; len(rest) > 0; i++ {
			n := 1 + r.IntN(len(rest))
			job := fmt.Sprintf("m%d", i)
			results = append(results, measurement(job, candidateCommit, rest[:n], 10+i, checkedBy("ck")))
			jobs = append(jobs, core.JobID(job))
			rest = rest[n:]
		}
		results = append(results, checkPass("ck", candidateCommit, 1), measurement("o", baseCommit, []float64{1, 2}, 3))
		r.Shuffle(len(results), func(i, j int) { results[i], results[j] = results[j], results[i] })

		var pooled []core.Measurement
		for _, m := range core.PoolMeasurements(results) {
			if m.Commit == candidateCommit {
				pooled = append(pooled, m)
			}
		}
		if len(pooled) != 1 {
			t.Fatalf("%d measurements of one commit and kind, want 1", len(pooled))
		}
		m := pooled[0]
		if !slices.Equal(m.Jobs, jobs) || !slices.Equal(m.Outcome.Samples, samples) || m.Outcome.CheckJob != "ck" {
			t.Fatalf("pooled jobs %v samples %v check %s, want jobs %v samples %v check ck", m.Jobs, m.Outcome.Samples, m.Outcome.CheckJob, jobs, samples)
		}
		verdict := compareCommits(t, append(slices.Clone(results), measurement("b", baseCommit, samples, 2)), 0).Verdict
		if verdict.CandidateSamples != uint32(len(m.Outcome.Samples)) || verdict.CandidateMean != core.Headline(m.Outcome.Samples) {
			t.Fatalf("compare counted %d samples with mean %v; the pooled measurement has %d with headline %v",
				verdict.CandidateSamples, verdict.CandidateMean, len(m.Outcome.Samples), core.Headline(m.Outcome.Samples))
		}
	}
}

// Measurements compare could not pool stay apart: another environment, task version or metric.
func TestMeasurementsOfAnotherKindStayApart(t *testing.T) {
	base := measurement("m1", candidateCommit, []float64{1, 2}, 1)
	for name, other := range map[string]core.Result{
		"environment":  measurement("m2", candidateCommit, []float64{3}, 2, inEnvironment(dockerEnv)),
		"task version": measurement("m2", candidateCommit, []float64{3}, 2, underTask("t2")),
		"metric":       measurement("m2", candidateCommit, []float64{3}, 2, scoring(core.Metric{Name: "latency", Direction: core.Minimize})),
	} {
		got := core.PoolMeasurements([]core.Result{other, base})
		if len(got) != 2 || !slices.Equal(got[0].Jobs, []core.JobID{"m1"}) || !slices.Equal(got[1].Jobs, []core.JobID{"m2"}) {
			t.Errorf("another %s: pooled %+v, want m1 and m2 apart, oldest first", name, got)
		}
	}
}
