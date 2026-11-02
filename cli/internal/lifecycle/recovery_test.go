package lifecycle_test

import (
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

var (
	aRef    = lifecycle.Ref{Store: "s", Job: "j1"}
	allJobs = []lifecycle.Finding{
		lifecycle.NoSuchJob{}, lifecycle.StoreAway{}, in(core.JobQueued), in(core.JobRunning), in(core.JobSucceeded),
		in(core.JobFailed), in(core.JobCancelled), in(core.JobLost),
	}
	presences = []lifecycle.Presence{
		lifecycle.RunNone{}, alive(lifecycle.RunParked), alive(lifecycle.RunRunning), alive(lifecycle.RunFinished),
		lifecycle.RunDead{Proof: lifecycle.ProofLockFree}, lifecycle.RunDead{Proof: lifecycle.ProofRebooted},
		lifecycle.RunUnknown{Limit: time.Hour},
	}
)

func in(state core.JobState) lifecycle.JobIn {
	return lifecycle.JobIn{State: state, SubmittedAt: epoch}
}

func alive(phase lifecycle.RunPhase) lifecycle.RunAlive {
	return lifecycle.RunAlive{Phase: phase, Limit: time.Hour}
}

// kinds names the requests, so that a table can say what it wants.
func kinds(reqs []lifecycle.Request) []string {
	out := make([]string, 0, len(reqs))
	for _, r := range reqs {
		out = append(out, fmt.Sprintf("%T", r)[len("lifecycle."):])
	}
	return out
}

// Every case of what a restart can find has one answer. The rows are grouped by what they protect:
// a store that is away and a supervisor that cannot be judged touch nothing.
func TestRecoveryDecidesEveryCase(t *testing.T) {
	want := func(finding lifecycle.Finding, presence lifecycle.Presence) []string {
		_, none := presence.(lifecycle.RunNone)
		_, unknown := presence.(lifecycle.RunUnknown)
		_, live := presence.(lifecycle.RunAlive)
		f, inStore := finding.(lifecycle.JobIn)
		queued, running := inStore && f.State == core.JobQueued, inStore && f.State == core.JobRunning
		_, away := finding.(lifecycle.StoreAway)
		switch {
		case away && none:
			return nil
		case away:
			return []string{"ArmRetry"}
		case unknown && !queued && !running:
			return nil
		case unknown:
			return []string{"ArmRetry"}
		case running && live:
			return []string{"Adopt"}
		case running && none:
			return []string{"ArmRetry"}
		case running:
			return []string{"Move"}
		case none && !queued:
			return nil
		case none:
			return []string{"LaunchRequest"}
		}
		return []string{"Discard"}
	}
	for _, finding := range allJobs {
		for _, presence := range presences {
			name := fmt.Sprintf("store %#v, supervisor %#v", finding, presence)
			s, reqs := lifecycle.Step(lifecycle.State{}, lifecycle.Recovered{Ref: aRef, Finding: finding, Supervisor: presence, At: epoch})
			_, more := lifecycle.Step(s, lifecycle.RecoveryDone{})
			got := kinds(append(reqs, more...))
			if !slices.Equal(got, want(finding, presence)) {
				t.Errorf("%s: requests = %v, want %v", name, got, want(finding, presence))
			}
		}
	}
}

// inDiscard is a machine that found a run left by a queued job, and is clearing it.
func inDiscard(t *testing.T) lifecycle.State {
	t.Helper()
	s, reqs := lifecycle.Step(lifecycle.State{}, lifecycle.Recovered{
		Ref: aRef, Finding: in(core.JobQueued), Supervisor: alive(lifecycle.RunRunning), At: epoch,
	})
	if got := kinds(reqs); !slices.Equal(got, []string{"Discard"}) {
		t.Fatalf("recovery asked for %v, want a Discard", got)
	}
	s, _ = lifecycle.Step(s, lifecycle.RecoveryDone{})
	return s
}

// A cancel during the clearing of a run is kept, and the job never runs.
func TestACancelWhileARunIsClearedStopsTheJob(t *testing.T) {
	s, reqs := lifecycle.Step(inDiscard(t), lifecycle.CancelAsked{Ref: aRef, State: core.JobQueued, Reason: "no", At: epoch})
	if len(reqs) != 0 {
		t.Fatalf("a cancel during the discard asked for %v, want nothing yet", kinds(reqs))
	}
	_, reqs = lifecycle.Step(s, lifecycle.Discarded{Ref: aRef})
	if got := kinds(reqs); !slices.Equal(got, []string{"Move"}) {
		t.Fatalf("after the discard: %v, want the cancel recorded and no launch", got)
	}
	if st := reqs[0].(lifecycle.Move).Settlement.Transition; st.To != core.JobCancelled || st.CancelReason != "no" {
		t.Fatalf("the settlement is %+v, want cancelled with the reason given", st)
	}
}

// A retry timer can outlive what it was armed for. When it fires it repeats nothing: what it would
// repeat is already in flight, and two discards of one run are two releases of a supervisor.
func TestARetryForAStepNoLongerWaitingRepeatsNothing(t *testing.T) {
	found := func(presence lifecycle.Presence) lifecycle.Recovered {
		return lifecycle.Recovered{Ref: aRef, Finding: in(core.JobQueued), Supervisor: presence, At: epoch}
	}
	s, reqs := lifecycle.Step(lifecycle.State{}, found(lifecycle.RunUnknown{Limit: time.Hour}))
	if got := kinds(reqs); !slices.Equal(got, []string{"ArmRetry"}) {
		t.Fatalf("a run that cannot be judged asked for %v, want a retry", got)
	}
	// A sweep looks before the retry fires, and now the run can be judged.
	s, reqs = lifecycle.Step(s, found(alive(lifecycle.RunRunning)))
	if got := kinds(reqs); !slices.Equal(got, []string{"Discard"}) {
		t.Fatalf("a second look asked for %v, want a Discard", got)
	}
	s, reqs = lifecycle.Step(s, lifecycle.RetryDue{Ref: aRef})
	if len(reqs) != 0 {
		t.Fatalf("a stale retry asked for %v, want nothing", kinds(reqs))
	}
	// The timer is spent, so the next failure arms a new one.
	_, reqs = lifecycle.Step(s, lifecycle.DiscardFailed{Ref: aRef})
	if got := kinds(reqs); !slices.Equal(got, []string{"ArmRetry"}) {
		t.Fatalf("the next failure asked for %v, want a retry", got)
	}
}

// A cancel that arrived while a job could not act on it reaches the supervisor before the command
// may start, on a second look as on a launch. A separate signal would race the start.
func TestASecondLookAdoptsWithTheCancelFirst(t *testing.T) {
	s, reqs := lifecycle.Step(lifecycle.State{}, lifecycle.Admitted{Ref: aRef, SubmittedAt: epoch})
	if len(reqs) != 1 {
		t.Fatalf("admitting: %v", reqs)
	}
	s, _ = lifecycle.Step(s, lifecycle.CancelAsked{Ref: aRef, Reason: "stop", State: core.JobQueued, SubmittedAt: epoch, At: epoch})
	s, _ = lifecycle.Step(s, lifecycle.Launched{Ref: aRef})
	s, _ = lifecycle.Step(s, lifecycle.FollowFailed{Ref: aRef, Cause: lifecycle.CauseSupervisorSilent, Failure: core.Failure{Message: "silent"}, At: epoch})
	s, _ = lifecycle.Step(s, lifecycle.StopFailed{Ref: aRef, Failure: core.Failure{Message: "silent"}})
	s, _ = lifecycle.Step(s, lifecycle.RetryDue{Ref: aRef})
	_, reqs = lifecycle.Step(s, lifecycle.Recovered{
		Ref: aRef, Finding: in(core.JobRunning), Supervisor: alive(lifecycle.RunRunning), At: epoch,
	})
	if len(reqs) != 1 {
		t.Fatalf("the second look asked for %v, want one Adopt", reqs)
	}
	adopt, ok := reqs[0].(lifecycle.Adopt)
	if !ok || !adopt.CancelFirst || adopt.CancelReason != "stop" {
		t.Fatalf("the second look asked for %#v, want an Adopt with the cancel first", reqs[0])
	}
}

// told is the jobs a waiter is told about: those in trouble and those held up by one.
func told(s lifecycle.State) []lifecycle.Ref {
	var out []lifecycle.Ref
	for _, t := range append(s.Troubled(), s.Waiting()...) {
		out = append(out, t.Ref)
	}
	return out
}

// A job whose store has it running and whose command has not started is not in trouble: it starts
// in its turn, as a queued job does. A daemon that restarts at that point must not tell whoever
// waits for the job that it cannot end. This holds before the machine has heard of every job, when
// a client can already ask, and after, and for a job of the same store queued behind it.
func TestAParkedJobIsNotToldAsTroubleToItsWaiters(t *testing.T) {
	behind := lifecycle.Ref{Store: aRef.Store, Job: "j2"}
	s, reqs := lifecycle.Step(lifecycle.Recovering(), lifecycle.Recovered{
		Ref: aRef, Finding: in(core.JobRunning), Supervisor: alive(lifecycle.RunParked), At: epoch,
	})
	if len(reqs) != 0 {
		t.Fatalf("recovering a parked job asked for %v, want nothing before RecoveryDone", kinds(reqs))
	}
	s, _ = lifecycle.Step(s, lifecycle.Admitted{Ref: behind, SubmittedAt: epoch.Add(time.Second)})
	if got := told(s); len(got) != 0 {
		t.Fatalf("while recovering, told %v about jobs that only wait for their turn", got)
	}
	s, reqs = lifecycle.Step(s, lifecycle.RecoveryDone{})
	if got := kinds(reqs); !slices.Equal(got, []string{"Adopt"}) {
		t.Fatalf("RecoveryDone asked for %v, want the parked job adopted", got)
	}
	if got := told(s); len(got) != 0 {
		t.Fatalf("after recovery, told %v about jobs that only wait for their turn", got)
	}
}

// A parked job that waits for a job the daemon cannot judge is told why, like a queued one, and not
// as a trouble of its own.
func TestAParkedJobBehindAJobInDoubtIsToldWhatItWaitsFor(t *testing.T) {
	doubtful := lifecycle.Ref{Store: lifecycle.StoreIDOf("/other", "t"), Job: "j9"}
	s, _ := lifecycle.Step(lifecycle.Recovering(), lifecycle.Recovered{
		Ref: doubtful, Finding: in(core.JobRunning), Supervisor: lifecycle.RunUnknown{Limit: time.Hour}, At: epoch,
	})
	s, _ = lifecycle.Step(s, lifecycle.Recovered{
		Ref: aRef, Finding: in(core.JobRunning), Supervisor: alive(lifecycle.RunParked), At: epoch,
	})
	s, reqs := lifecycle.Step(s, lifecycle.RecoveryDone{})
	if got := kinds(reqs); len(got) != 0 {
		t.Fatalf("the parked job started (%v) while a command that cannot be judged may hold the machine", got)
	}
	for _, tr := range s.Troubled() {
		if tr.Ref == aRef {
			t.Fatalf("the parked job is told as a trouble of its own: %+v", tr)
		}
	}
	if w := s.Waiting(); len(w) != 1 || w[0].Ref != aRef {
		t.Fatalf("waiting = %+v, want the parked job with the reason", w)
	}
}
