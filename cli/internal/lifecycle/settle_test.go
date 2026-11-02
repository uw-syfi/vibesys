package lifecycle_test

import (
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

func anyOutcome(w *world) lifecycle.Outcome {
	kinds := []core.ExecKind{core.ExecExited, core.ExecTimedOut, core.ExecCancelled, core.ExecServiceNotReady, core.ExecServiceExited, core.ExecUnknown, "a kind from a newer build", ""}
	o := lifecycle.Outcome{
		End:          lifecycle.Ran{Exec: core.ExecOutcome{Kind: kinds[w.r.IntN(len(kinds))], ExitCode: w.r.IntN(5) - 1}},
		CancelAsked:  w.r.IntN(2) == 0,
		CancelReason: []string{"", "stop", "a longer reason"}[w.r.IntN(3)],
	}
	switch w.r.IntN(12) {
	case 0:
		o.End = lifecycle.NotAttempted{Failure: core.Failure{Kind: core.FailureUnavailable, Message: "no such image"}}
	case 1:
		o.End = lifecycle.Rejected{Reason: "the benchmark wrote no result"}
	case 2:
		o.End = nil
	}
	return o
}

// Whatever a supervisor reports, the settlement is a transition core accepts for a running job
// that ends, and success needs the report to say the command exited with zero.
func TestSettleOnlyCallsAJobSucceededWhenItSaysSo(t *testing.T) {
	w := newWorld(t, 1, 1)
	for range 2000 {
		o := anyOutcome(w)
		st := lifecycle.Settle("j1", core.JobRunning, o, epoch)
		job := core.Job{ID: "j1", State: core.JobRunning, Spec: core.JobSpec{Settings: core.CommandSettings{}}}
		if _, err := job.Apply(st.Transition); err != nil {
			t.Fatalf("outcome %+v settles as %+v, which core refuses: %v", o, st.Transition, err)
		}
		if !st.Transition.To.Final() {
			t.Fatalf("outcome %+v settles as %q, which is not final", o, st.Transition.To)
		}
		ran, attempted := o.End.(lifecycle.Ran)
		wantSuccess := attempted && ran.Exec.Kind == core.ExecExited && ran.Exec.ExitCode == 0
		if got := st.Transition.To == core.JobSucceeded; got != wantSuccess {
			t.Fatalf("outcome %+v settles as %q", o, st.Transition.To)
		}
		wantCancel := attempted && ran.Exec.Kind == core.ExecCancelled && o.CancelAsked
		if got := st.Transition.To == core.JobCancelled; got != wantCancel {
			t.Fatalf("outcome %+v settles as %q", o, st.Transition.To)
		}
		if wantCancel && st.Transition.CancelReason != o.CancelReason {
			t.Fatalf("a cancelled job keeps the reason %q, got %q", o.CancelReason, st.Transition.CancelReason)
		}
		if st.Transition.To == core.JobLost && st.Log == "" {
			t.Fatalf("a lost job says nothing about why: %+v", st)
		}
	}
}

// A command that was cancelled and a command that was ended by something else are different
// jobs: the first is cancelled and the second is lost, and an empty reason does not change that.
func TestACancelWithNoReasonIsStillACancel(t *testing.T) {
	asked := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: lifecycle.Ran{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}, CancelAsked: true}, epoch)
	if asked.Transition.To != core.JobCancelled {
		t.Fatalf("a cancel with no reason settles as %q", asked.Transition.To)
	}
	stopped := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: lifecycle.Ran{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}}, epoch)
	if stopped.Transition.To != core.JobLost {
		t.Fatalf("a command ended with no cancel behind it settles as %q", stopped.Transition.To)
	}
}

func TestAwaitIsOverWhenTheJobsItWaitsForAreFinal(t *testing.T) {
	w := newWorld(t, 2, 1)
	all := core.JobStates()
	for range 2000 {
		n := 1 + w.r.IntN(5)
		states := make([]core.JobState, n)
		finals, successes := 0, 0
		for i := range states {
			states[i] = all[w.r.IntN(len(all))]
			if states[i].Final() {
				finals++
				if states[i] == core.JobSucceeded {
					successes++
				}
			}
		}
		done, ok := lifecycle.Await(states, false)
		if done != (finals == n) {
			t.Fatalf("wait for all %v: done = %v", states, done)
		}
		doneAny, okAny := lifecycle.Await(states, true)
		if doneAny != (finals > 0) {
			t.Fatalf("wait for any %v: done = %v", states, doneAny)
		}
		if ok != (successes == finals) || okAny != ok {
			t.Fatalf("wait %v: succeeded = %v and %v, want %v", states, ok, okAny, successes == finals)
		}
	}
}

// A job that passed saves its result with its success, and a measurement keeps its check pass. A
// rejected job fails and says why.
func TestAPassedJobSavesItsResultAndARejectedOneSaysWhy(t *testing.T) {
	result := core.Result{Job: "j1", Outcome: core.MeasureOutcome{CheckJob: "j0"}}
	passed := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: lifecycle.Passed{Result: result}}, epoch)
	if passed.Transition.To != core.JobSucceeded || passed.Transition.Result == nil {
		t.Fatalf("a passed job settles as %+v", passed.Transition)
	}
	// The check pass is stored once, in the result, and the job takes it from there.
	if passed.Transition.CheckJob != "" {
		t.Fatalf("the settlement copies the check pass %q beside the result that holds it", passed.Transition.CheckJob)
	}
	rejected := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: lifecycle.Rejected{Reason: "no result"}}, epoch)
	if rejected.Transition.To != core.JobFailed || !strings.Contains(rejected.Log, "no result") {
		t.Fatalf("a rejected job settles as %+v", rejected)
	}
}

// planJob is a running check or measure job, and a result that is a result of it.
func planJob(kind core.JobKind) (core.Job, core.Result) {
	fp := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
	spec := core.JobSpec{Commit: core.Commit(strings.Repeat("ab", 20))}
	var outcome core.Outcome
	switch kind {
	case core.JobCheck:
		spec.Settings = core.CheckSettings{}
		outcome = core.CheckOutcome{Duration: time.Second}
	default:
		spec.Settings = core.MeasureSettings{Samples: 2}
		outcome = core.MeasureOutcome{Metric: core.Metric{Name: "ops", Direction: core.Maximize}, Samples: []float64{1, 2}, CheckJob: "j0"}
	}
	job := core.Job{ID: "j1", State: core.JobRunning, Key: "k1", Spec: spec, Environment: &fp}
	res := core.Result{
		Job: "j1", Key: "k1", Task: "t", TaskVersion: "v1", Commit: spec.Commit,
		SchemaVersion: core.SchemaVersion, Environment: fp, Outcome: outcome, RecordedAt: epoch,
	}
	return job, res
}

// Whatever a supervisor reports about a check or a measure job, the settlement is one core
// accepts, and a result is saved exactly when the job passed: a rejected result, a failed step, a
// cancel and a loss save nothing, and a job that passed takes its check pass from its result.
func TestAPlanJobSavesAResultExactlyWhenItPassed(t *testing.T) {
	w := newWorld(t, 3, 1)
	for range 4000 {
		kind := []core.JobKind{core.JobCheck, core.JobMeasure}[w.r.IntN(2)]
		job, res := planJob(kind)
		o := anyOutcome(w)
		if w.r.IntN(3) == 0 {
			o.End = lifecycle.Passed{Result: res}
		}
		_, passed := o.End.(lifecycle.Passed)

		st := lifecycle.Settle(job.ID, core.JobRunning, o, epoch)
		if ran, ok := o.End.(lifecycle.Ran); ok && ran.Exec.Kind == core.ExecExited && ran.Exec.ExitCode == 0 {
			// A plan job that only exited 0 is not a pass: core refuses it, which keeps a result
			// from being missing. A supervisor reports a pass only as Passed.
			if _, err := job.Apply(st.Transition); err == nil {
				t.Fatalf("outcome %+v settles a %s job as succeeded without a result, and core accepts it", o, kind)
			}
			continue
		}
		got, err := job.Apply(st.Transition)
		if err != nil {
			t.Fatalf("outcome %+v settles a %s job as %+v, which core refuses: %v", o, kind, st.Transition, err)
		}
		if saved := st.Transition.Result != nil; saved != passed {
			t.Fatalf("outcome %+v: result saved = %v, passed = %v", o, saved, passed)
		}
		if passed && (got.State != core.JobSucceeded || got.ResultJob != job.ID) {
			t.Fatalf("a job that passed is %+v", got)
		}
		if m, ok := res.Outcome.(core.MeasureOutcome); ok && passed && got.CheckJob != m.CheckJob {
			t.Fatalf("a measure that passed links check %q, want %q", got.CheckJob, m.CheckJob)
		}
		if _, rejected := o.End.(lifecycle.Rejected); rejected && (got.State != core.JobFailed || st.Log == "") {
			t.Fatalf("a rejected result settles as %+v", st)
		}
	}
}
