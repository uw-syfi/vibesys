package app_test

import (
	"errors"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// The daemon decides how a wait ended and names the job that decided it.
func TestAWaitEndsWithTheOutcomeTheJobsDecide(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	ok := submit(t, a, "exit", "0")
	bad := submit(t, a, "exit", "1")
	var emitted []core.JobID
	all, err := a.Wait(ctxFor(t), scope, core.WaitFor{IDs: []core.JobID{ok.ID, bad.ID}}, func(j core.Job) error {
		emitted = append(emitted, j.ID)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if all.End != core.WaitSomeFailed || all.DecidedBy != bad.ID || len(all.Jobs) != 2 || all.Jobs[0].ID != ok.ID {
		t.Fatalf("a wait on a success and a failure ended %+v", all)
	}
	if len(emitted) < 2 {
		t.Fatalf("the wait sent %v, not each job's state", emitted)
	}
	cases := []struct {
		ids  []core.JobID
		any  bool
		want core.WaitEnd
		by   core.JobID
	}{
		{[]core.JobID{ok.ID}, false, core.WaitAllSucceeded, ""},
		{[]core.JobID{ok.ID, bad.ID}, true, core.WaitFirstFailed, bad.ID},
		{[]core.JobID{ok.ID}, true, core.WaitFirstSucceeded, ok.ID},
	}
	for _, tc := range cases {
		got, err := a.Wait(ctxFor(t), scope, core.WaitFor{IDs: tc.ids, Any: tc.any}, func(core.Job) error { return nil })
		if err != nil || got.End != tc.want || got.DecidedBy != tc.by {
			t.Errorf("wait(%v, any=%v) = %+v, %v; want %s by %q", tc.ids, tc.any, got, err, tc.want, tc.by)
		}
	}
}

// A timeout ends the wait with TIMED_OUT and the jobs as they were, and cancels nothing: the job
// goes on and succeeds.
func TestAWaitThatTimesOutSaysSoAndCancelsNothing(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	held := submit(t, a, "hold", "g", ";", "exit", "0")
	// The rig's timers fire a timeout of up to a second at once, and never a longer one.
	got, err := a.Wait(ctxFor(t), scope, core.WaitFor{IDs: []core.JobID{held.ID}, Timeout: time.Second}, func(core.Job) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	if got.End != core.WaitTimedOut || got.DecidedBy != "" || len(got.Jobs) != 1 || got.Jobs[0].State.Final() {
		t.Fatalf("a wait that timed out ended %+v", got)
	}
	w.gates.Release("g")
	if j := awaitFinal(t, a, held.ID); j.State != core.JobSucceeded {
		t.Fatalf("the job a wait timed out on ended %s", j.State)
	}
}

func TestAWaitOnNothingOrAnUnknownJobIsRefused(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	submit(t, a, "exit", "0")
	for name, req := range map[string]core.WaitFor{
		"no job":           {},
		"negative timeout": {IDs: []core.JobID{"j1"}, Timeout: -time.Second},
	} {
		var f *core.Failure
		if _, err := a.Wait(ctxFor(t), scope, req, func(core.Job) error { return nil }); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v, want invalid input", name, err)
		}
	}
	var f *core.Failure
	if _, err := a.Wait(ctxFor(t), scope, core.WaitFor{IDs: []core.JobID{"nobody"}}, func(core.Job) error { return nil }); !errors.As(err, &f) || f.Kind != core.FailureNotFound {
		t.Errorf("a wait on an unknown job: %v, want not found", err)
	}
}
