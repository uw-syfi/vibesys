package lifecycle_test

import (
	"testing"

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
