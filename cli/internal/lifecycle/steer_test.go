package lifecycle_test

import (
	"fmt"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

var (
	steerStore = lifecycle.StoreIDOf("/repo", "task")
	steerEpoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	steerDrain = &core.Drain{Agent: "lead", Reason: "a new build", Since: steerEpoch}
)

func steerRef(n int) lifecycle.Ref {
	return lifecycle.Ref{Store: steerStore, Job: core.JobID(fmt.Sprintf("j%d", n))}
}

// fourSlots is a machine of four slots, on which every queued job could start at once.
func fourSlots() lifecycle.Scheduling {
	return lifecycle.Scheduling{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 4}}}
}

func admitted(n int, steering lifecycle.Steering) lifecycle.Admitted {
	return lifecycle.Admitted{
		Ref: steerRef(n), SubmittedAt: steerEpoch.Add(time.Duration(n) * time.Second), Policy: core.RerunAfterProvenDeath, Steering: steering,
		Work: lifecycle.Work{Kind: core.JobCheck, Options: schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1})},
	}
}

func launched(reqs []lifecycle.Request) []lifecycle.Ref {
	var out []lifecycle.Ref
	for _, r := range reqs {
		if l, ok := r.(lifecycle.LaunchRequest); ok {
			out = append(out, l.Ref)
		}
	}
	return out
}

// A daemon that starts draining, as its runtime directory says before recovery ends, starts none of
// the queued jobs it recovers, and says of each that the daemon drains; once it resumes, they start
// in their turn. This is the rollout: drain, let what runs end, restart on a new build, resume.
func TestADaemonThatRestartsDrainingStartsNothingUntilItResumes(t *testing.T) {
	s := lifecycle.RecoveringWith(fourSlots())
	s, _ = lifecycle.Step(s, lifecycle.Drained{Drain: steerDrain})
	for n := 1; n <= 3; n++ {
		var reqs []lifecycle.Request
		s, reqs = lifecycle.Step(s, lifecycle.Recovered{
			Ref: steerRef(n), Finding: lifecycle.JobIn{State: core.JobQueued, SubmittedAt: steerEpoch, Policy: core.RerunAfterProvenDeath, Work: admitted(n, lifecycle.Steering{}).Work},
			Supervisor: lifecycle.RunNone{}, At: steerEpoch,
		})
		if got := launched(reqs); len(got) != 0 {
			t.Fatalf("recovery launched %v", got)
		}
	}
	s, reqs := lifecycle.Step(s, lifecycle.RecoveryDone{})
	if got := launched(reqs); len(got) != 0 {
		t.Fatalf("a draining daemon launched %v when its recovery ended", got)
	}
	queue := s.Queue()
	if len(queue) != 3 {
		t.Fatalf("the queue is %+v, want the three jobs waiting", queue)
	}
	for _, q := range queue {
		if q.Reason.Kind != core.WaitHeld || q.Reason.Detail != steerDrain.WaitDetail() {
			t.Errorf("%s waits as %+v, want held by the drain", q.Ref, q.Reason)
		}
	}
	if s.Draining() == nil {
		t.Fatalf("the machine does not say it drains")
	}
	s, reqs = lifecycle.Step(s, lifecycle.Drained{})
	if got := launched(reqs); len(got) != 3 {
		t.Fatalf("resuming launched %v, want the three jobs", got)
	}
	if s.Draining() != nil {
		t.Fatalf("the machine still drains after it resumed")
	}
}

// Over generated sequences of submissions, holds, releases, drains, resumes and ends, the machine
// never launches a job that is held or while it drains, and a job that is neither starts as soon as
// a slot is free. A submission the machine has already, as a sweep of the store may post, never
// changes how the job is steered: a look at the store older than a hold cannot undo it.
func TestTheMachineNeverLaunchesAHeldJobOrWhileItDrains(t *testing.T) {
	for seed := range uint64(300) {
		r := rand.New(rand.NewPCG(seed, 7))
		s, _ := lifecycle.Step(lifecycle.RecoveringWith(fourSlots()), lifecycle.RecoveryDone{})
		held := map[lifecycle.Ref]bool{}
		draining := false
		running, ended := map[lifecycle.Ref]bool{}, map[lifecycle.Ref]bool{}
		next := 0
		for step := range 60 {
			var e lifecycle.Event
			switch op := r.IntN(7); {
			case op < 2 || next == 0:
				next++
				steering := lifecycle.Steering{}
				if r.IntN(3) == 0 {
					steering.Hold = &core.JobHold{Agent: "lead", At: steerEpoch}
					held[steerRef(next)] = true
				}
				e = admitted(next, steering)
			case op == 2:
				ref := steerRef(1 + r.IntN(next))
				steering := lifecycle.Steering{}
				held[ref] = r.IntN(2) == 0
				if held[ref] {
					steering.Hold = &core.JobHold{Agent: "lead", At: steerEpoch}
				}
				e = lifecycle.Steered{Ref: ref, Steering: steering}
			case op == 3:
				// A sweep posts again a job its store has queued, as the store had it before any hold.
				ref := steerRef(1 + r.IntN(next))
				if ended[ref] {
					continue
				}
				e = lifecycle.Admitted{Ref: ref, SubmittedAt: steerEpoch, Work: admitted(1, lifecycle.Steering{}).Work}
			case op == 4:
				draining = r.IntN(2) == 0
				var d *core.Drain
				if draining {
					d = steerDrain
				}
				e = lifecycle.Drained{Drain: d}
			default:
				var ref lifecycle.Ref
				for running := range running {
					ref = running
					break
				}
				if ref == (lifecycle.Ref{}) {
					continue
				}
				// It runs, ends, and its end is recorded, which frees its slot.
				delete(running, ref)
				ended[ref] = true
				s, _ = lifecycle.Step(s, lifecycle.Launched{Ref: ref})
				s, _ = lifecycle.Step(s, lifecycle.Finished{Ref: ref, At: steerEpoch})
				e = lifecycle.Closed{Ref: ref}
			}
			var reqs []lifecycle.Request
			s, reqs = lifecycle.Step(s, e)
			for _, ref := range launched(reqs) {
				if held[ref] || draining {
					t.Fatalf("seed %d, step %d: %s launched while held %v, draining %v", seed, step, ref, held[ref], draining)
				}
				running[ref] = true
			}
			queued, active := s.Counts()
			if !draining && queued > 0 && active < 4 {
				for _, q := range s.Queue() {
					if !held[q.Ref] {
						t.Fatalf("seed %d, step %d: %s waits as %+v with a slot free", seed, step, q.Ref, q.Reason)
					}
				}
			}
		}
	}
}
