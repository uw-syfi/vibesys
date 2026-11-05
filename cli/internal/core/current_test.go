package core_test

import (
	"fmt"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestResultHasNothingToReportBeforeAPromotion(t *testing.T) {
	results := []core.Result{measurement("m", candidateCommit, clearWin, 5), checkPass("c", candidateCommit, 5)}
	got, ok := core.CurrentResultAt("", results).(core.NoCurrentResult)
	if !ok || got.Reason != core.NoHead || got.Head != "" {
		t.Errorf("%+v, want no head", got)
	}
}

// The head's number is its latest measurement that rests on a passing check of the same commit,
// task version and environment. When it was taken does not matter: the measurement a promotion
// rests on, taken before the promotion, is a measurement of the head.
func TestResultIsTheHeadsLatestMeasurementOnAPassingCheck(t *testing.T) {
	cases := []struct {
		name    string
		results []core.Result
		want    string // the job reported, or "" for no current result
	}{
		{"nothing saved", nil, ""},
		{"checked in its own job", []core.Result{measurement("m", candidateCommit, clearWin, 5)}, "m"},
		{"on a saved check pass", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", candidateCommit, 3)}, "m"},
		{"the check it names is not saved", []core.Result{measurement("m", candidateCommit, clearWin, 20, checkedBy("c"))}, ""},
		{"it names no check", []core.Result{measurement("m", candidateCommit, clearWin, 20, checkedBy(""))}, ""},
		{"the check passed another commit", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", commitN(9), 15)}, ""},
		{"the check passed in another environment", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", candidateCommit, 15, inEnvironment(dockerEnv))}, ""},
		{"the check passed under another task version", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", candidateCommit, 15, underTask("t0"))}, ""},
		{"the latest qualifying measurement wins", []core.Result{
			measurement("old", candidateCommit, clearWin, 12), measurement("new", candidateCommit, clearWin, 30), measurement("mid", candidateCommit, clearWin, 20)}, "new"},
		{"a later measurement in another environment is the latest", []core.Result{
			measurement("host", candidateCommit, clearWin, 12), measurement("docker", candidateCommit, clearWin, 30, inEnvironment(dockerEnv))}, "docker"},
		{"a later measurement with no check does not hide an earlier checked one", []core.Result{
			measurement("checked", candidateCommit, clearWin, 12), measurement("bare", candidateCommit, clearWin, 30, checkedBy(""))}, "checked"},
		{"another commit's measurements are not the head's", []core.Result{
			measurement("m", baseCommit, clearWin, 30), measurement("n", commitN(9), clearWin, 30)}, ""},
		{"checks alone are no number", []core.Result{checkPass("c", candidateCommit, 30)}, ""},
	}
	for _, c := range cases {
		answer := core.CurrentResultAt(candidateCommit, c.results)
		if c.want == "" {
			none, ok := answer.(core.NoCurrentResult)
			if !ok || none.Reason != core.HeadNotMeasured || none.Head != candidateCommit {
				t.Errorf("%s: %+v, want a head that is not measured", c.name, answer)
			}
			continue
		}
		got, ok := answer.(core.CurrentResult)
		if !ok || got.Result.Job != core.JobID(c.want) || got.Head != candidateCommit {
			t.Errorf("%s: %+v, want job %s", c.name, answer, c.want)
		}
	}
}

// With any mix of results of the head and of other commits, checked in their own job, on a
// separate pass in the same or another environment, or on none, the answer is the latest
// measurement of the head on a passing check of its commit, task version and environment, or
// there is none. The order the results come in does not matter.
func TestResultIsTheLatestQualifyingMeasurement(t *testing.T) {
	r := newRand(51)
	for range properties {
		var results []core.Result
		wantMinute, wantJob := -1, ""
		for i := range r.IntN(8) {
			job := fmt.Sprintf("m%d", i)
			minute := r.IntN(60)
			commit := candidateCommit
			if r.IntN(5) == 0 {
				commit = commitN(1)
			}
			var m core.Result
			qualifies := commit == candidateCommit
			switch r.IntN(4) {
			case 0: // checked inside its own job
				m = measurement(job, commit, anySamples(r), minute)
			case 1: // checked by a separate job of the same commit and environment, at any time
				m = measurement(job, commit, anySamples(r), minute, checkedBy("c"+job))
				results = append(results, checkPass("c"+job, commit, r.IntN(60)))
			case 2: // checked by a separate job in another environment
				m = measurement(job, commit, anySamples(r), minute, checkedBy("c"+job))
				results = append(results, checkPass("c"+job, commit, r.IntN(60), inEnvironment(dockerEnv)))
				qualifies = false
			default: // names no check
				m = measurement(job, commit, anySamples(r), minute, checkedBy(""))
				qualifies = false
			}
			results = append(results, m)
			// Ties on the minute fall to the later job name, as the order of measurements goes.
			if qualifies && (minute > wantMinute || (minute == wantMinute && job > wantJob)) {
				wantMinute, wantJob = minute, job
			}
		}
		r.Shuffle(len(results), func(i, j int) { results[i], results[j] = results[j], results[i] })
		answer := core.CurrentResultAt(candidateCommit, results)
		if wantJob == "" {
			if none, ok := answer.(core.NoCurrentResult); !ok || none.Reason != core.HeadNotMeasured || none.Head != candidateCommit {
				t.Fatalf("%+v, want no current result", answer)
			}
			continue
		}
		if got, ok := answer.(core.CurrentResult); !ok || string(got.Result.Job) != wantJob || got.Head != candidateCommit {
			t.Fatalf("%+v, want job %s", answer, wantJob)
		}
	}
}
