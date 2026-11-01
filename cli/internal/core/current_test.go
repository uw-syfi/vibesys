package core_test

import (
	"fmt"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// promoted is a head that moved to candidateCommit at minute 10.
func promoted() []core.Promotion {
	return []core.Promotion{
		{Commit: baseCommit, At: at(0)},
		{Commit: candidateCommit, PreviousHead: baseCommit, At: at(10)},
	}
}

func current(t testing.TB, promotions []core.Promotion, results []core.Result) core.CurrentResult {
	t.Helper()
	got, ok := core.CurrentResultOf(promotions, results).(core.CurrentResult)
	if !ok {
		t.Fatalf("CurrentResultOf = %+v, want a result", core.CurrentResultOf(promotions, results))
	}
	return got
}

func TestResultHasNothingToReportBeforeAPromotion(t *testing.T) {
	results := []core.Result{measurement("m", candidateCommit, clearWin, 5), checkPass("c", candidateCommit, 5)}
	for _, promotions := range [][]core.Promotion{nil, {}} {
		got, ok := core.CurrentResultOf(promotions, results).(core.NoCurrentResult)
		if !ok || got.Reason != core.NoHead || got.Head != "" {
			t.Errorf("%+v, want no head", got)
		}
	}
}

func TestResultNeedsAMeasurementAndACheckAfterTheLastPromotion(t *testing.T) {
	cases := []struct {
		name    string
		results []core.Result
		want    string // the job reported, or "" for no current result
	}{
		{"nothing saved", nil, ""},
		{"measured before the move", []core.Result{measurement("m", candidateCommit, clearWin, 9)}, ""},
		{"measured at the instant of the move", []core.Result{measurement("m", candidateCommit, clearWin, 10)}, ""},
		{"measured after the move", []core.Result{measurement("m", candidateCommit, clearWin, 11)}, "m"},
		{"the check it names was from before the move", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", candidateCommit, 9)}, ""},
		{"the check it names was at the move", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", candidateCommit, 10)}, ""},
		{"the check it names was after the move", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", candidateCommit, 11)}, "m"},
		{"the check it names is not saved", []core.Result{measurement("m", candidateCommit, clearWin, 20, checkedBy("c"))}, ""},
		{"it names no check", []core.Result{measurement("m", candidateCommit, clearWin, 20, checkedBy(""))}, ""},
		{"the check passed another commit", []core.Result{
			measurement("m", candidateCommit, clearWin, 20, checkedBy("c")), checkPass("c", commitN(9), 15)}, ""},
		{"the latest qualifying measurement wins", []core.Result{
			measurement("old", candidateCommit, clearWin, 12), measurement("new", candidateCommit, clearWin, 30), measurement("mid", candidateCommit, clearWin, 20)}, "new"},
		{"a stale one does not hide a fresh one", []core.Result{
			measurement("fresh", candidateCommit, clearWin, 12), measurement("before", candidateCommit, clearWin, 5)}, "fresh"},
		{"a later measurement with no check does not hide an earlier checked one", []core.Result{
			measurement("checked", candidateCommit, clearWin, 12), measurement("bare", candidateCommit, clearWin, 30, checkedBy(""))}, "checked"},
		{"another commit's measurements are not the head's", []core.Result{
			measurement("m", baseCommit, clearWin, 30), measurement("n", commitN(9), clearWin, 30)}, ""},
		{"checks alone are no number", []core.Result{checkPass("c", candidateCommit, 30)}, ""},
	}
	for _, c := range cases {
		answer := core.CurrentResultOf(promoted(), c.results)
		if c.want == "" {
			none, ok := answer.(core.NoCurrentResult)
			if !ok || none.Reason != core.NotMeasuredSincePromotion || none.Head != candidateCommit {
				t.Errorf("%s: %+v, want a head that was not measured since it moved", c.name, answer)
			}
			continue
		}
		got, ok := answer.(core.CurrentResult)
		if !ok || got.Result.Job != core.JobID(c.want) || got.Head != candidateCommit {
			t.Errorf("%s: %+v, want job %s", c.name, answer, c.want)
		}
	}
}

// No set of saved results can answer `result` if none of them was recorded after the head last
// moved, however many there are and however well they were checked: the cache cannot satisfy it.
func TestSavedResultsFromBeforeTheMoveNeverAnswer(t *testing.T) {
	r := newRand(50)
	for range properties {
		movedAt := 10 + r.IntN(30)
		promotions := []core.Promotion{{Commit: candidateCommit, At: at(movedAt)}}
		var results []core.Result
		for i := range r.IntN(8) {
			job := fmt.Sprintf("m%d", i)
			results = append(results, measurement(job, candidateCommit, anySamples(r), r.IntN(movedAt+1)))
			results = append(results, checkPass(fmt.Sprintf("c%d", i), candidateCommit, r.IntN(movedAt+1)))
		}
		if got, ok := core.CurrentResultOf(promotions, results).(core.NoCurrentResult); !ok || got.Reason != core.NotMeasuredSincePromotion {
			t.Fatalf("answered from results recorded no later than the move: %+v", got)
		}
	}
}

// With any mix of results around the move, the answer is the latest measurement that was
// recorded after the move on a check that was too, or there is none.
func TestResultIsTheLatestQualifyingMeasurement(t *testing.T) {
	r := newRand(51)
	for range properties {
		movedAt := 10 + r.IntN(30)
		promotions := []core.Promotion{{Commit: commitN(1), At: at(0)}, {Commit: candidateCommit, At: at(movedAt)}}
		var results []core.Result
		wantMinute, wantJob := -1, ""
		for i := range r.IntN(8) {
			job := fmt.Sprintf("m%d", i)
			minute := r.IntN(2 * movedAt)
			commit := candidateCommit
			if r.IntN(5) == 0 {
				commit = commitN(1)
			}
			checkMinute := r.IntN(2 * movedAt)
			var m core.Result
			switch r.IntN(3) {
			case 0: // checked inside its own job
				m, checkMinute = measurement(job, commit, anySamples(r), minute), minute
			case 1: // checked by a separate job
				m = measurement(job, commit, anySamples(r), minute, checkedBy("c"+job))
				results = append(results, checkPass("c"+job, commit, checkMinute))
			default: // names no check
				m = measurement(job, commit, anySamples(r), minute, checkedBy(""))
				checkMinute = -1
			}
			results = append(results, m)
			// Ties on the minute fall to the later job name, as the order of measurements goes.
			qualifies := commit == candidateCommit && checkMinute > movedAt && minute > movedAt
			if qualifies && (minute > wantMinute || (minute == wantMinute && job > wantJob)) {
				wantMinute, wantJob = minute, job
			}
		}
		r.Shuffle(len(results), func(i, j int) { results[i], results[j] = results[j], results[i] })
		answer := core.CurrentResultOf(promotions, results)
		if wantJob == "" {
			if none, ok := answer.(core.NoCurrentResult); !ok || none.Reason != core.NotMeasuredSincePromotion || none.Head != candidateCommit {
				t.Fatalf("%+v, want no current result", answer)
			}
			continue
		}
		if got, ok := answer.(core.CurrentResult); !ok || string(got.Result.Job) != wantJob || got.Head != candidateCommit {
			t.Fatalf("%+v, want job %s", answer, wantJob)
		}
	}
}
