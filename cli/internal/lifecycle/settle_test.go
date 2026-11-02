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
		Exec:         core.ExecOutcome{Kind: kinds[w.r.IntN(len(kinds))], ExitCode: w.r.IntN(5) - 1},
		CancelAsked:  w.r.IntN(2) == 0,
		CancelReason: []string{"", "stop", "a longer reason"}[w.r.IntN(3)],
	}
	if w.r.IntN(6) == 0 {
		o.NotAttempted = &core.Failure{Kind: core.FailureUnavailable, Message: "no such image"}
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
		wantSuccess := o.NotAttempted == nil && o.Exec.Kind == core.ExecExited && o.Exec.ExitCode == 0
		if got := st.Transition.To == core.JobSucceeded; got != wantSuccess {
			t.Fatalf("outcome %+v settles as %q", o, st.Transition.To)
		}
		wantCancel := o.NotAttempted == nil && o.Exec.Kind == core.ExecCancelled && o.CancelAsked
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
	asked := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecCancelled}, CancelAsked: true}, epoch)
	if asked.Transition.To != core.JobCancelled {
		t.Fatalf("a cancel with no reason settles as %q", asked.Transition.To)
	}
	stopped := lifecycle.Settle("j1", core.JobRunning, lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}, epoch)
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
// accepts, and a result is saved exactly when the job succeeded: a rejected result, a failed
// step, a cancel and a loss save nothing, and a job that passed never settles without its result.
func TestAPlanJobSavesAResultExactlyWhenItPassed(t *testing.T) {
	w := newWorld(t, 3, 1)
	for range 4000 {
		kind := []core.JobKind{core.JobCheck, core.JobMeasure}[w.r.IntN(2)]
		job, res := planJob(kind)
		o := anyOutcome(w)
		if w.r.IntN(2) == 0 {
			o.Result = &res
		}
		o.Rejected = w.r.IntN(5) == 0
		if w.r.IntN(3) == 0 {
			o.Reason = "the check failed"
		}
		passed := o.NotAttempted == nil && !o.Rejected && o.Exec.Kind == core.ExecExited && o.Exec.ExitCode == 0 && o.Result != nil

		st := lifecycle.Settle(job.ID, core.JobRunning, o, epoch)
		if !passed && st.Transition.To == core.JobSucceeded {
			// A command that exited 0 with no result is a success only for a command job: a plan
			// job without its result is refused by core, which is what keeps it from being saved.
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
		if o.Rejected && (got.State != core.JobFailed || st.Log == "") {
			t.Fatalf("a rejected result settles as %+v", st)
		}
		if got.State == core.JobFailed && o.Reason != "" && !strings.Contains(st.Log, o.Reason) {
			t.Fatalf("a failed job's output %q does not say why: %q", st.Log, o.Reason)
		}
	}
}
