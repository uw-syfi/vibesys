package lifecycle_test

import (
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// execKinds are the endings a supervisor may report, with ones this build does not know.
var execKinds = []core.ExecKind{core.ExecExited, core.ExecTimedOut, core.ExecCancelled, core.ExecServiceNotReady, core.ExecServiceExited, "a kind from a newer build", ""}

func anyExec(w *world) core.ExecOutcome {
	return core.ExecOutcome{Kind: execKinds[w.r.IntN(len(execKinds))], ExitCode: w.r.IntN(5) - 1}
}

func withCancel(w *world, o lifecycle.Outcome) lifecycle.Outcome {
	o.CancelAsked = w.r.IntN(2) == 0
	o.CancelReason = []string{"", "stop", "a longer reason"}[w.r.IntN(3)]
	return o
}

// anyCommandOutcome is any report a supervisor of a command job can make.
func anyCommandOutcome(w *world) lifecycle.Outcome {
	f := core.Failure{Kind: core.FailureUnavailable, Message: "no such image"}
	var a core.Attempt
	switch w.r.IntN(8) {
	case 0:
		a = core.NotStarted{Failure: f}
	case 1:
		a = core.Unobserved{Failure: f}
	case 2:
		return withCancel(w, lifecycle.Outcome{})
	case 3:
		// An ending with no attempt.
	default:
		a = core.Ended{Exec: anyExec(w)}
	}
	return withCancel(w, lifecycle.Outcome{End: lifecycle.CommandEnded{Attempt: a}})
}

// anyFlowOutcome is any report a supervisor of a check or a measure job can make, which passes
// with res.
func anyFlowOutcome(w *world, res core.Result) lifecycle.Outcome {
	f := core.Failure{Kind: core.FailureUnavailable, Message: "no such image"}
	var v lifecycle.Verdict
	switch w.r.IntN(9) {
	case 0:
		v = lifecycle.NotAttempted{Failure: f}
	case 1:
		v = lifecycle.Lost{Failure: f}
	case 2:
		v = lifecycle.Rejected{Reason: "the benchmark wrote no result"}
	case 3:
		return withCancel(w, lifecycle.Outcome{})
	case 4:
		// An ending with no verdict.
	case 5, 6:
		v = lifecycle.Passed{Result: res}
	default:
		v = lifecycle.Stopped{Exec: anyExec(w)}
	}
	return withCancel(w, lifecycle.Outcome{End: lifecycle.FlowEnded{Verdict: v}})
}

// attemptOf is the attempt a command job's outcome reports, nil for none.
func attemptOf(o lifecycle.Outcome) core.Attempt {
	if end, ok := o.End.(lifecycle.CommandEnded); ok {
		return end.Attempt
	}
	return nil
}

// Whatever a supervisor of a command job reports, the settlement is a transition core accepts for
// a running job that ends, and success needs the report to say the command was seen to exit 0. A
// command that never started failed, and one that started and was not seen to end is lost: the
// two never settle alike.
func TestSettleOnlyCallsACommandJobSucceededWhenItSaysSo(t *testing.T) {
	w := newWorld(t, 1, 1)
	for range 2000 {
		o := anyCommandOutcome(w)
		st := lifecycle.Settle("j1", core.JobRunning, o, epoch)
		job := core.Job{ID: "j1", State: core.JobRunning, Spec: core.JobSpec{Settings: core.CommandSettings{}}}
		if _, err := job.Apply(st.Transition); err != nil {
			t.Fatalf("outcome %+v settles as %+v, which core refuses: %v", o, st.Transition, err)
		}
		if !st.Transition.To.Final() {
			t.Fatalf("outcome %+v settles as %q, which is not final", o, st.Transition.To)
		}
		ended, sawEnd := attemptOf(o).(core.Ended)
		wantSuccess := sawEnd && ended.Exec.Kind == core.ExecExited && ended.Exec.ExitCode == 0
		if got := st.Transition.To == core.JobSucceeded; got != wantSuccess {
			t.Fatalf("outcome %+v settles as %q", o, st.Transition.To)
		}
		wantCancel := sawEnd && ended.Exec.Kind == core.ExecCancelled && o.CancelAsked
		if got := st.Transition.To == core.JobCancelled; got != wantCancel {
			t.Fatalf("outcome %+v settles as %q", o, st.Transition.To)
		}
		if wantCancel && st.Transition.CancelReason != o.CancelReason {
			t.Fatalf("a cancelled job keeps the reason %q, got %q", o.CancelReason, st.Transition.CancelReason)
		}
		switch attemptOf(o).(type) {
		case core.NotStarted:
			if st.Transition.To != core.JobFailed || !strings.Contains(st.Log, "could not be run") {
				t.Fatalf("a command that never started settles as %+v", st)
			}
		case core.Unobserved:
			if st.Transition.To != core.JobLost || strings.Contains(st.Log, "could not be run") {
				t.Fatalf("a command that ran and was not seen to end settles as %+v", st)
			}
		}
		if st.Transition.To == core.JobLost && st.Log == "" {
			t.Fatalf("a lost job says nothing about why: %+v", st)
		}
	}
}

// A command that was cancelled and a command that was ended by something else are different
// jobs: the first is cancelled and the second is lost, and an empty reason does not change that.
func TestACancelWithNoReasonIsStillACancel(t *testing.T) {
	cancelled := lifecycle.CommandEnded{Attempt: core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}}
	asked := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: cancelled, CancelAsked: true}, epoch)
	if asked.Transition.To != core.JobCancelled {
		t.Fatalf("a cancel with no reason settles as %q", asked.Transition.To)
	}
	stopped := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: cancelled}, epoch)
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
	passed := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: lifecycle.FlowEnded{Verdict: lifecycle.Passed{Result: result}}}, epoch)
	if passed.Transition.To != core.JobSucceeded || passed.Transition.Result == nil {
		t.Fatalf("a passed job settles as %+v", passed.Transition)
	}
	// The check pass is stored once, in the result, and the job takes it from there.
	if passed.Transition.CheckJob != "" {
		t.Fatalf("the settlement copies the check pass %q beside the result that holds it", passed.Transition.CheckJob)
	}
	rejected := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{End: lifecycle.FlowEnded{Verdict: lifecycle.Rejected{Reason: "no result"}}}, epoch)
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
// step that exited 0 and still stopped the job, a cancel and a loss save nothing, and a job that
// passed takes its check pass from its result.
func TestAPlanJobSavesAResultExactlyWhenItPassed(t *testing.T) {
	w := newWorld(t, 3, 1)
	for range 4000 {
		kind := []core.JobKind{core.JobCheck, core.JobMeasure}[w.r.IntN(2)]
		job, res := planJob(kind)
		o := anyFlowOutcome(w, res)
		var verdict lifecycle.Verdict
		if end, ok := o.End.(lifecycle.FlowEnded); ok {
			verdict = end.Verdict
		}
		_, passed := verdict.(lifecycle.Passed)

		st := lifecycle.Settle(job.ID, core.JobRunning, o, epoch)
		got, err := job.Apply(st.Transition)
		if err != nil {
			t.Fatalf("outcome %+v settles a %s job as %+v, which core refuses: %v", o, kind, st.Transition, err)
		}
		if saved := st.Transition.Result != nil; saved != passed {
			t.Fatalf("outcome %+v: result saved = %v, passed = %v", o, saved, passed)
		}
		if got := st.Transition.To == core.JobSucceeded; got != passed {
			t.Fatalf("outcome %+v settles as %q", o, st.Transition.To)
		}
		if passed && (got.State != core.JobSucceeded || got.ResultJob != job.ID) {
			t.Fatalf("a job that passed is %+v", got)
		}
		if m, ok := res.Outcome.(core.MeasureOutcome); ok && passed && got.CheckJob != m.CheckJob {
			t.Fatalf("a measure that passed links check %q, want %q", got.CheckJob, m.CheckJob)
		}
		switch verdict.(type) {
		case lifecycle.Rejected:
			if got.State != core.JobFailed || st.Log == "" {
				t.Fatalf("a rejected result settles as %+v", st)
			}
		case lifecycle.NotAttempted:
			if got.State != core.JobFailed || !strings.Contains(st.Log, "could not be run") {
				t.Fatalf("a step that never started settles as %+v", st)
			}
		case lifecycle.Lost:
			if got.State != core.JobLost || strings.Contains(st.Log, "could not be run") {
				t.Fatalf("a step that ran and was not seen to end settles as %+v", st)
			}
		}
	}
}
