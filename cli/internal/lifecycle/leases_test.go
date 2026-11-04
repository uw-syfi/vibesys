package lifecycle_test

import (
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// leaseState is a machine of two slots of the default placement and one GPU.
func leaseState(t *testing.T) lifecycle.State {
	t.Helper()
	s, _ := lifecycle.Step(lifecycle.RecoveringWith(lifecycle.Scheduling{
		Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 2}, {ID: gpu, Capacity: 1}},
	}), lifecycle.RecoveryDone{})
	return s
}

var (
	measureWork = lifecycle.Work{Kind: core.JobMeasure, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: true})}
	commandWork = lifecycle.Work{Kind: core.JobCommand, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1})}
	gpuWork     = lifecycle.Work{Kind: core.JobCommand, Options: schedule.Only(schedule.Claim{Resource: gpu, Amount: 1, Exclusive: true})}
	other       = core.LeaseHolder{User: "bob", Host: "host-b", PID: 4242, Job: "their-measure"}
)

func ref(job string) lifecycle.Ref { return lifecycle.Ref{Store: "s", Job: core.JobID(job)} }

func admit(t *testing.T, s lifecycle.State, job string, w lifecycle.Work, at int) (lifecycle.State, []lifecycle.Request) {
	t.Helper()
	return lifecycle.Step(s, lifecycle.Admitted{Ref: ref(job), SubmittedAt: epoch.Add(time.Duration(at) * time.Second), Work: w})
}

func TestALaunchTakesTheLeasesOfWhatItHoldsAlone(t *testing.T) {
	s := leaseState(t)
	s, reqs := admit(t, s, "c", commandWork, 0)
	if want := []lifecycle.Request{lifecycle.LaunchRequest{Ref: ref("c")}}; !reflect.DeepEqual(reqs, want) {
		t.Fatalf("a command that shares its slot asks for %+v, want no lease", reqs)
	}
	_, reqs = admit(t, s, "g", gpuWork, 1)
	if want := []lifecycle.Request{lifecycle.LaunchRequest{Ref: ref("g"), Leases: []schedule.ResourceID{gpu}}}; !reflect.DeepEqual(reqs, want) {
		t.Fatalf("a job that holds the GPU alone asks for %+v, want its lease", reqs)
	}
}

// TestAJobRefusedALeaseWaitsKeepingItsTurn has another daemon hold the lease of the placement: the
// measure waits, naming the holder, and keeps its turn, so a later command that shares the
// placement waits behind it while a job on another resource starts. When the retry comes, the
// measure launches again.
func TestAJobRefusedALeaseWaitsKeepingItsTurn(t *testing.T) {
	s := leaseState(t)
	s, reqs := admit(t, s, "m", measureWork, 0)
	if len(reqs) != 1 {
		t.Fatalf("the measure asked for %+v", reqs)
	}
	s, reqs = lifecycle.Step(s, lifecycle.LeaseRefused{Ref: ref("m"), Resource: schedule.DefaultPlacement, Holder: other, At: epoch})
	if want := []lifecycle.Request{lifecycle.ArmRetry{Ref: ref("m")}}; !reflect.DeepEqual(reqs, want) {
		t.Fatalf("a refused lease asked for %+v, want a retry", reqs)
	}
	q := s.Queue()
	if len(q) != 1 || q[0].Ref != ref("m") || q[0].Reason.Kind != core.WaitLeased || q[0].Reason.Elsewhere == nil || *q[0].Reason.Elsewhere != other {
		t.Fatalf("the queue says %+v, want the measure waiting for bob's lease", q)
	}
	s, reqs = admit(t, s, "c", commandWork, 1)
	if len(reqs) != 0 {
		t.Fatalf("a command started on the placement a waiting measure keeps its turn on: %+v", reqs)
	}
	s, reqs = admit(t, s, "g", gpuWork, 2)
	if want := []lifecycle.Request{lifecycle.LaunchRequest{Ref: ref("g"), Leases: []schedule.ResourceID{gpu}}}; !reflect.DeepEqual(reqs, want) {
		t.Fatalf("a job on another resource asked for %+v", reqs)
	}
	_, reqs = lifecycle.Step(s, lifecycle.RetryDue{Ref: ref("m")})
	if want := []lifecycle.Request{lifecycle.LaunchRequest{Ref: ref("m"), Leases: []schedule.ResourceID{schedule.DefaultPlacement}}}; !reflect.DeepEqual(reqs, want) {
		t.Fatalf("the retry asked for %+v, want the measure launched again", reqs)
	}
}

// TestACancelWhileTakingALeaseIsTheEnd has a measure cancelled while its leases are being taken:
// the refusal ends it as cancelled, since it never ran.
func TestACancelWhileTakingALeaseIsTheEnd(t *testing.T) {
	s := leaseState(t)
	s, _ = admit(t, s, "m", measureWork, 0)
	s, _ = lifecycle.Step(s, lifecycle.CancelAsked{Ref: ref("m"), State: core.JobQueued, Reason: "no", At: epoch})
	_, reqs := lifecycle.Step(s, lifecycle.LeaseRefused{Ref: ref("m"), Resource: schedule.DefaultPlacement, Holder: other, At: epoch})
	if len(reqs) != 1 {
		t.Fatalf("the refusal asked for %+v", reqs)
	}
	mv, ok := reqs[0].(lifecycle.Move)
	if !ok || mv.Settlement.Transition.To != core.JobCancelled || mv.Settlement.Transition.From != core.JobQueued {
		t.Fatalf("the refusal asked for %+v, want the job moved from queued to cancelled", reqs)
	}
}

// TestALateRefusalChangesNothing has a refusal come for a job that is not being launched.
func TestALateRefusalChangesNothing(t *testing.T) {
	s := leaseState(t)
	s, _ = admit(t, s, "m", measureWork, 0)
	s, _ = lifecycle.Step(s, lifecycle.Launched{Ref: ref("m")})
	after, reqs := lifecycle.Step(s, lifecycle.LeaseRefused{Ref: ref("m"), Resource: schedule.DefaultPlacement, Holder: other, At: epoch})
	if len(reqs) != 0 || !reflect.DeepEqual(after.Held(), s.Held()) {
		t.Fatalf("a late refusal asked for %+v and left %+v", reqs, after.Held())
	}
}
