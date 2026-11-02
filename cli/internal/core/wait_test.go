package core_test

import (
	"fmt"
	"reflect"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// waitJobs draws one to five jobs in random states, with distinct ids in the order asked. Some draws
// use a state this build does not know, the shape a lost or newer value takes.
func waitJobs(r interface{ IntN(int) int }) []core.Job {
	states := append(core.JobStates(), core.JobState("from_a_newer_build"))
	jobs := make([]core.Job, 1+r.IntN(5))
	for i := range jobs {
		jobs[i] = core.Job{ID: core.JobID(fmt.Sprintf("j%d", i)), State: states[r.IntN(len(states))]}
	}
	return jobs
}

// The rule a wait ends by, stated once more, simply: a wait for all is over when every job is
// final, a wait for any when one is, and it succeeded when every final job did.
func TestAWaitEndsExactlyWhenTheJobsItWaitsForAreFinal(t *testing.T) {
	r := newRand(21)
	for range 2000 {
		jobs := waitJobs(r)
		final, succeeded := 0, 0
		for _, j := range jobs {
			if j.State.Final() {
				final++
				if j.State == core.JobSucceeded {
					succeeded++
				}
			}
		}
		for _, any := range []bool{false, true} {
			o, over := core.DecideWait(slices.Clone(jobs), any)
			wantOver := final == len(jobs) || (any && final > 0)
			if over != wantOver {
				t.Fatalf("wait(any=%v) on %v: over = %v, want %v", any, jobs, over, wantOver)
			}
			if !over {
				if o.End != "" {
					t.Fatalf("a wait that is not over has the end %q", o.End)
				}
				continue
			}
			success := o.End == core.WaitAllSucceeded || o.End == core.WaitFirstSucceeded
			if success != (succeeded == final) {
				t.Fatalf("wait(any=%v) on %v ended %q with %d of %d final jobs succeeded", any, jobs, o.End, succeeded, final)
			}
			if any != (o.End == core.WaitFirstSucceeded || o.End == core.WaitFirstFailed) {
				t.Fatalf("wait(any=%v) ended %q", any, o.End)
			}
			if !reflect.DeepEqual(o.Jobs, jobs) {
				t.Fatalf("the outcome's jobs %v are not the jobs %v in the order asked", o.Jobs, jobs)
			}
			checkDecider(t, o, jobs)
		}
	}
}

// checkDecider holds DecidedBy to the first job, in the order asked, that decided the end.
func checkDecider(t *testing.T, o core.WaitOutcome, jobs []core.Job) {
	t.Helper()
	first := func(match func(core.Job) bool) core.JobID {
		for _, j := range jobs {
			if match(j) {
				return j.ID
			}
		}
		return ""
	}
	failed := func(j core.Job) bool { return j.State.Final() && j.State != core.JobSucceeded }
	var want core.JobID
	switch o.End {
	case core.WaitSomeFailed, core.WaitFirstFailed:
		want = first(failed)
	case core.WaitFirstSucceeded:
		want = first(func(j core.Job) bool { return j.State.Final() })
	case core.WaitAllSucceeded, core.WaitTimedOut:
	}
	if o.DecidedBy != want {
		t.Fatalf("a wait that ended %q on %v was decided by %q, want %q", o.End, jobs, o.DecidedBy, want)
	}
}

// A state this build cannot read is not final, so it never ends a wait, let alone as a success.
func TestAStateTheDaemonCannotReadNeverEndsAWait(t *testing.T) {
	unknown := []core.Job{{ID: "j1", State: "from_a_newer_build"}}
	for _, any := range []bool{false, true} {
		if o, over := core.DecideWait(unknown, any); over {
			t.Fatalf("wait(any=%v) on an unreadable state ended %q", any, o.End)
		}
	}
	if _, over := core.DecideWait(nil, false); over {
		t.Fatal("a wait on no jobs is over")
	}
}

func TestATimedOutWaitKeepsTheJobsAndNamesNoDecider(t *testing.T) {
	jobs := []core.Job{{ID: "j1", State: core.JobRunning}}
	o := core.TimedOutWait(jobs)
	if o.End != core.WaitTimedOut || o.DecidedBy != "" || !reflect.DeepEqual(o.Jobs, jobs) {
		t.Fatalf("a timed out wait is %+v", o)
	}
}
