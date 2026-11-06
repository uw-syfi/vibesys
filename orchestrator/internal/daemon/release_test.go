package daemon_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// A sidecar outlives the daemon and the session it holds: after the session closes, the real one
// stays up for a while so the daemon can read the close. So a run that ends is only over when
// every sidecar it launched is gone, and the daemon, which launched them, is who ends them.

// worker is how a spawned worker's session behaves toward the daemon.
type worker string

const (
	// workerIdle finishes its first turn and waits.
	workerIdle worker = "idle"
	// workerBusy is in a turn that ends only when it is interrupted.
	workerBusy worker = "busy"
	// workerDeaf answers no command at all, as a wedged agent does.
	workerDeaf worker = "deaf"
	// workerHandedOff posts its handoff after its first turn.
	workerHandedOff worker = "handed off"
)

var workers = []worker{workerIdle, workerBusy, workerDeaf, workerHandedOff}

// ending is how a run is brought to its end.
type ending string

const (
	endOperator ending = "the operator stops the run"
	endDeadline ending = "the run's deadline passes"
)

// world is a run being driven to its end over the Rig's Fakes.
type world struct {
	t       *testing.T
	r       *daemontest.Rig
	run     core.Run
	lead    core.Agent
	kinds   map[core.AgentID]worker
	spawned []core.AgentID
}

// newWorld starts a run whose lead is idle and whose Fake sidecars behave as each agent's worker
// says. The lead's first turn is busy when leadBusy is set.
func newWorld(t *testing.T, deadline time.Duration, leadBusy bool) *world {
	t.Helper()
	w := &world{t: t, r: daemontest.NewRig(t), kinds: map[core.AgentID]worker{}}
	w.r.Sidecars.React = func(s *daemontest.Session, cmd *pb.SessionCommand) {
		kind := w.kinds[s.Launch.Session.Agent]
		if s.Launch.Role == core.RoleLead && leadBusy {
			kind = workerBusy
		}
		switch {
		case kind == workerDeaf:
		case kind == workerBusy && cmd.GetStartTurn() != nil:
		default:
			daemontest.DefaultReaction(s, cmd)
		}
	}
	cmd := startRun("start")
	if deadline > 0 {
		cmd.GetStartRun().Deadline = timestamppb.New(w.r.Clock.Now().Add(deadline))
	}
	receipt := dispatch(t, w.r, cmd)
	w.r.Settle(t)
	w.run, w.lead = leadOf(w.r, receipt)
	return w
}

// spawn adds a worker that behaves as kind says.
func (w *world) spawn(kind worker) {
	w.t.Helper()
	token := w.r.Sidecars.Session(core.Session{Agent: w.lead.ID, N: 1}).Credentials.GetToken()
	id := fmt.Sprintf("spawn-%d", len(w.spawned))
	resp, err := w.r.AgentService.Dispatch(agentContext(w.lead.ID, 1, token), &pb.DispatchRequest{Command: &pb.Command{Id: id, Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
		RunId: string(w.run.ID), Role: pb.AgentRole_AGENT_ROLE_WORKER, Brief: "work",
	}}}})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		w.t.Fatalf("spawn %s: %v, %v", kind, resp, err)
	}
	agent := core.AgentID(resp.GetReceipt().GetAgentId())
	w.kinds[agent] = kind
	w.spawned = append(w.spawned, agent)
	w.r.Settle(w.t)
	if kind == workerHandedOff {
		session := w.r.Sidecars.Session(core.Session{Agent: agent, N: 1})
		_, err := w.r.AgentService.Dispatch(agentContext(agent, 1, session.Credentials.GetToken()), &pb.DispatchRequest{Command: &pb.Command{Id: "handoff-" + id, Body: &pb.Command_PostHandoff{PostHandoff: &pb.PostHandoff{Handoff: &pb.Handoff{Claim: "done", Body: &pb.Handoff_Worker{Worker: &pb.WorkerHandoff{Outcome: &pb.WorkerHandoff_NoCandidate{NoCandidate: &pb.NoCandidate{Blocker: "none"}}}}}}}}})
		if err != nil {
			w.t.Fatalf("handoff: %v", err)
		}
		w.r.Settle(w.t)
	}
}

// end brings the run to its end the given way, restarting the daemon right after the decision when
// restart is set (a crash before it carried out anything), and lets everything run out.
func (w *world) end(how ending, restart bool) {
	w.t.Helper()
	switch how {
	case endOperator:
		dispatch(w.t, w.r, &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(w.run.ID)}}})
	case endDeadline:
		w.r.Clock.Advance(time.Hour)
	}
	if restart {
		w.r.Restart(w.t)
	}
	for range 4 {
		w.r.Settle(w.t)
		w.r.Clock.Advance(core.StopGrace + time.Second)
	}
	w.r.Settle(w.t)
}

// requireNoSidecarLeft fails for a run that is not over, and for any sidecar still running.
func (w *world) requireNoSidecarLeft(label string) {
	w.t.Helper()
	run, _ := w.r.Daemon.State().Run(w.run.ID)
	if !run.Status.Final() {
		w.t.Fatalf("%s: the run is %s, want it over", label, run.Status)
	}
	for _, s := range w.r.Sidecars.Sessions() {
		if !s.Dead() {
			w.t.Fatalf("%s: the sidecar of %s/%d still runs although the run is %s", label, s.Launch.Session.Agent, s.Launch.Session.N, run.Status)
		}
	}
}

// Stopping a run ends the sidecar of its lead and of each worker that was still going, not only the
// ones that were stopped one by one before.
func TestStoppingARunEndsTheSidecarsOfItsLeadAndItsWorkers(t *testing.T) {
	w := newWorld(t, 0, false)
	w.spawn(workerBusy)
	w.spawn(workerIdle)
	w.end(endOperator, false)
	w.requireNoSidecarLeft("operator stop")
}

// A worker stopped on its own before the run ended has its sidecar ended too: the two paths agree.
func TestAStoppedWorkersSidecarEndsAsTheRunsDo(t *testing.T) {
	w := newWorld(t, 0, false)
	w.spawn(workerIdle)
	token := w.r.Sidecars.Session(core.Session{Agent: w.lead.ID, N: 1}).Credentials.GetToken()
	dispatchAs := func(id string, body *pb.Command) {
		_, err := w.r.AgentService.Dispatch(agentContext(w.lead.ID, 1, token), &pb.DispatchRequest{Command: body})
		if err != nil {
			t.Fatalf("%s: %v", id, err)
		}
	}
	dispatchAs("stop-agent", &pb.Command{Id: "stop-agent", Body: &pb.Command_StopAgent{StopAgent: &pb.StopAgent{AgentId: string(w.spawned[0])}}})
	w.r.Settle(t)
	if s := w.r.Sidecars.Session(core.Session{Agent: w.spawned[0], N: 1}); !s.Dead() {
		t.Fatal("the sidecar of the stopped worker still runs")
	}
}

// Whatever the sessions of a run are doing when it ends (a turn under way, a session that answers
// nothing, a handoff posted, no worker at all), and however it ends, even with the daemon killed
// right after it decided, no sidecar is left running once the run is over.
func TestAnyRunThatEndsLeavesNoSidecarRunning(t *testing.T) {
	for seed := uint64(1); seed <= 80; seed++ {
		rng := rand.New(rand.NewPCG(seed, 7))
		how := []ending{endOperator, endDeadline}[rng.IntN(2)]
		deadline := time.Duration(0)
		if how == endDeadline {
			deadline = 30 * time.Minute
		}
		leadBusy := rng.IntN(2) == 0
		w := newWorld(t, deadline, leadBusy)
		var chosen []worker
		for range rng.IntN(4) {
			kind := workers[rng.IntN(len(workers))]
			chosen = append(chosen, kind)
			w.spawn(kind)
		}
		restart := rng.IntN(2) == 0
		w.end(how, restart)
		w.requireNoSidecarLeft(fmt.Sprintf("seed %d (lead busy %v, workers %v, %s, restart %v)", seed, leadBusy, chosen, how, restart))
	}
}

// stepAll steps the daemon until it moves nothing, tolerating the errors a step reports, for tests
// that inject failures.
func stepAll(t *testing.T, r *daemontest.Rig) {
	t.Helper()
	for range 1000 {
		r.Hub.Flush()
		if moved, _ := r.Daemon.Step(context.Background()); !moved {
			return
		}
	}
	t.Fatal("the daemon never settled")
}

func alive(r *daemontest.Rig) int {
	n := 0
	for _, s := range r.Sidecars.Sessions() {
		if !s.Dead() {
			n++
		}
	}
	return n
}

// A kill that fails is tried again by the next step, and the run is over meanwhile: the end of a
// session is not held back by the end of its process.
func TestAKillThatFailsIsRetriedUntilTheSidecarEnds(t *testing.T) {
	w := newWorld(t, 0, false)
	w.spawn(workerIdle)
	w.r.Sidecars.KillFailures = 10
	dispatch(t, w.r, &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(w.run.ID)}}})
	stepAll(t, w.r)
	run, _ := w.r.Daemon.State().Run(w.run.ID)
	if !run.Status.Final() || alive(w.r) == 0 {
		t.Fatalf("run %s with %d sidecars running: the failing kills should have left some", run.Status, alive(w.r))
	}
	for range 5 {
		stepAll(t, w.r)
	}
	w.requireNoSidecarLeft("after the kills were retried")
}

// A sidecar the daemon that ended the run could not end, because it stopped first, is ended by the
// next one when it starts.
func TestASidecarLeftByADaemonThatStoppedIsEndedByTheNext(t *testing.T) {
	w := newWorld(t, 0, false)
	w.spawn(workerIdle)
	w.r.Sidecars.KillFailures = 1000
	dispatch(t, w.r, &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(w.run.ID)}}})
	stepAll(t, w.r)
	if alive(w.r) == 0 {
		t.Fatal("the failing kills should have left sidecars running")
	}
	w.r.Sidecars.KillFailures = 0
	w.r.Restart(t)
	w.r.Settle(t)
	w.requireNoSidecarLeft("after a restart")
}

// A run's working copies are released once it is over and never before: the Fake is asked for the
// run's subagents only, the copies it keeps are recorded on the run, and a daemon that restarts right
// after deciding the end still releases them.
func TestAnEndedRunReleasesItsWorkersCheckoutsAndRecordsTheOnesKept(t *testing.T) {
	for _, restart := range []bool{false, true} {
		w := newWorld(t, 0, false)
		w.spawn(workerIdle)
		w.spawn(workerIdle)
		w.r.Checkouts.Dirty = map[core.AgentID]bool{w.spawned[1]: true}
		if got := w.r.Checkouts.Releases(); len(got) != 0 {
			t.Fatalf("a running run released checkouts: %v", got)
		}
		w.end(endOperator, restart)
		w.requireNoSidecarLeft("operator stop")
		calls := w.r.Checkouts.Releases()
		if len(calls) == 0 {
			t.Fatalf("restart=%v: nothing was released", restart)
		}
		for _, call := range calls {
			for _, a := range call {
				if a == w.lead.ID {
					t.Fatalf("the lead was offered for release: %v", call)
				}
			}
		}
		run, _ := w.r.Daemon.State().Run(w.run.ID)
		if len(run.Kept) != 1 || run.Kept[0].Agent != w.spawned[1] {
			t.Fatalf("restart=%v: kept %+v, want only %s", restart, run.Kept, w.spawned[1])
		}
	}
}
