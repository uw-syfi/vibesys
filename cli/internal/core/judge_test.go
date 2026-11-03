package core_test

import (
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestJudgeGivesTheVerdictCompareCommitsGivesForAnyMeasurements(t *testing.T) {
	r := newRand(60)
	for range properties {
		results := sides(anySamples(r), anySamples(r))
		noise := 0.0
		if r.IntN(3) == 0 {
			noise = r.Float64() * 0.1
		}
		got := core.Judge(candidateCommit, baseCommit, results, noise)
		want, err := core.CompareCommits(core.CompareRequest{Commit: candidateCommit, Against: baseCommit, Results: results, RelativeNoise: noise})
		if err != nil {
			if got.Refused == nil || got.Comparison != nil || got.Verdict() != nil || *got.Refused != core.AsFailure(err) {
				t.Fatalf("Judge = %+v, want refused with %v", got, err)
			}
			continue
		}
		if got.Refused != nil || got.Comparison == nil || !reflect.DeepEqual(*got.Comparison, want) || !reflect.DeepEqual(*got.Verdict(), want.Verdict) {
			t.Fatalf("Judge = %+v, want the comparison %+v", got, want)
		}
		if got.Commit != candidateCommit || got.Against != baseCommit {
			t.Fatalf("Judge names %s against %s, want %s against %s", got.Commit, got.Against, candidateCommit, baseCommit)
		}
	}
}

func TestJudgeRefusesWhatCannotBeComparedAndSaysWhy(t *testing.T) {
	measured := measurement("base", baseCommit, []float64{1, 2, 3}, 1)
	other := measurement("cand", candidateCommit, []float64{1, 2, 3}, 2, func(r *core.Result) { r.Environment = dockerEnv })
	for name, tc := range map[string]struct {
		results []core.Result
		want    core.FailureKind
	}{
		"the candidate is not measured": {[]core.Result{measured}, core.FailureNotMeasured},
		"the head is not measured":      {[]core.Result{measurement("cand", candidateCommit, []float64{1, 2, 3}, 2)}, core.FailureNotMeasured},
		"two environments":              {[]core.Result{measured, other}, core.FailureEnvironmentMismatch},
	} {
		t.Run(name, func(t *testing.T) {
			got := core.Judge(candidateCommit, baseCommit, tc.results, 0)
			if got.Refused == nil || got.Refused.Kind != tc.want || got.Comparison != nil {
				t.Errorf("Judge = %+v, want refused as %s", got, tc.want)
			}
		})
	}
}

func TestSubmittersAreOneForEachRunAndAgentInTheOrderTheyFirstSubmitted(t *testing.T) {
	r := newRand(61)
	runs := []core.RunID{"", "run-a", "run-b"}
	agents := []string{"", "lead", "worker"}
	for range properties {
		var have []core.Submitter
		first := map[[2]string]int{}
		var order [][2]string
		for i := range 1 + r.IntN(12) {
			s := core.Submitter{Run: runs[r.IntN(len(runs))], Agent: agents[r.IntN(len(agents))], At: at(i)}
			before := append([]core.Submitter(nil), have...)
			next, added := core.WithSubmitter(have, s)
			key := [2]string{string(s.Run), s.Agent}
			if _, seen := first[key]; seen == added {
				t.Fatalf("WithSubmitter(%+v) added = %v, but the caller had submitted before = %v", s, added, seen)
			}
			if added {
				first[key] = i
				order = append(order, key)
			}
			if !reflect.DeepEqual(have, before) {
				t.Fatalf("WithSubmitter changed the slice it was given: %+v, was %+v", have, before)
			}
			have = next
		}
		if len(have) != len(order) {
			t.Fatalf("%d submitters, want one for each of %v", len(have), order)
		}
		for i, key := range order {
			if have[i].Run != core.RunID(key[0]) || have[i].Agent != key[1] || !have[i].At.Equal(at(first[key])) {
				t.Fatalf("submitter %d = %+v, want %v first at %v", i, have[i], key, at(first[key]))
			}
		}
	}
}

func TestSubmitterOfNamesTheCallerAndTheTimeOfTheSubmission(t *testing.T) {
	sub := core.Submission{Context: core.CallContext{Run: "run-a", Agent: "lead", Harness: "claude-code"}, At: at(3)}
	if got, want := core.SubmitterOf(sub), (core.Submitter{Run: "run-a", Agent: "lead", At: at(3)}); got != want {
		t.Errorf("SubmitterOf = %+v, want %+v", got, want)
	}
}
