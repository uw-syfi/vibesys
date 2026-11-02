package lifecycle_test

import (
	"fmt"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

var (
	aRef      = lifecycle.Ref{Store: "s", Job: "j1"}
	storeHas  = []core.JobState{"", core.JobQueued, core.JobRunning, core.JobSucceeded, core.JobFailed, core.JobCancelled, core.JobLost}
	presences = []lifecycle.Presence{lifecycle.PresenceNone, lifecycle.PresenceAlive, lifecycle.PresenceGone, lifecycle.PresenceUnknown}
)

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
	want := func(state core.JobState, presence lifecycle.Presence, away bool) []string {
		final := state != core.JobQueued && state != core.JobRunning
		switch {
		case away && (presence == lifecycle.PresenceAlive || presence == lifecycle.PresenceUnknown):
			return []string{"ArmRetry"}
		case away:
			return nil
		case presence == lifecycle.PresenceUnknown && final:
			return nil
		case presence == lifecycle.PresenceUnknown:
			return []string{"ArmRetry"}
		case state == core.JobRunning && presence == lifecycle.PresenceAlive:
			return []string{"Adopt"}
		case state == core.JobRunning:
			return []string{"Move"}
		case presence == lifecycle.PresenceNone && final:
			return nil
		case presence == lifecycle.PresenceNone:
			return []string{"LaunchRequest"}
		}
		return []string{"Discard"}
	}
	for _, state := range storeHas {
		for _, presence := range presences {
			for _, away := range []bool{false, true} {
				name := fmt.Sprintf("store %q, supervisor %s, away %v", state, presence, away)
				s, reqs := lifecycle.Step(lifecycle.State{}, lifecycle.Recovered{
					Ref: aRef, State: state, SubmittedAt: epoch, Supervisor: presence, Away: away, At: epoch,
				})
				_, more := lifecycle.Step(s, lifecycle.RecoveryDone{})
				got := kinds(append(reqs, more...))
				if !slices.Equal(got, want(state, presence, away)) {
					t.Errorf("%s: requests = %v, want %v", name, got, want(state, presence, away))
				}
			}
		}
	}
}

// inDiscard is a machine that found a run left by a queued job, and is clearing it.
func inDiscard(t *testing.T) lifecycle.State {
	t.Helper()
	s, reqs := lifecycle.Step(lifecycle.State{}, lifecycle.Recovered{
		Ref: aRef, State: core.JobQueued, SubmittedAt: epoch, Supervisor: lifecycle.PresenceAlive, At: epoch,
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
		return lifecycle.Recovered{Ref: aRef, State: core.JobQueued, SubmittedAt: epoch, Supervisor: presence, At: epoch}
	}
	s, reqs := lifecycle.Step(lifecycle.State{}, found(lifecycle.PresenceUnknown))
	if got := kinds(reqs); !slices.Equal(got, []string{"ArmRetry"}) {
		t.Fatalf("a run that cannot be judged asked for %v, want a retry", got)
	}
	// A sweep looks before the retry fires, and now the run can be judged.
	s, reqs = lifecycle.Step(s, found(lifecycle.PresenceAlive))
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
