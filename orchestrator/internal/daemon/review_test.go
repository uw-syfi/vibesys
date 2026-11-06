package daemon_test

import (
	"context"
	"errors"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
)

// step runs one step and returns its error, for tests that expect a fault to show.
func step(r *daemontest.Rig) error {
	r.Hub.Flush()
	_, err := r.Daemon.Step(context.Background())
	return err
}

func leadOf(r *daemontest.Rig, receipt *pb.Receipt) (core.Run, core.Agent) {
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	lead, _ := r.Daemon.State().Agent(run.Lead)
	return run, lead
}

func command(session *daemontest.Session) []string {
	var out []string
	for _, c := range session.Received {
		switch c.GetBody().(type) {
		case *pb.SessionCommand_StartTurn:
			out = append(out, "start")
		case *pb.SessionCommand_Steer:
			out = append(out, "steer")
		case *pb.SessionCommand_Interrupt:
			out = append(out, "interrupt")
		case *pb.SessionCommand_Close:
			out = append(out, "close")
		}
	}
	return out
}

// Finding 1. A daemon that stops between committing a session's launch and carrying it out must
// not take the run with it, wherever in the first steps it stops.
func TestARestartBeforeTheLaunchIsCarriedOutLosesNothing(t *testing.T) {
	for stepsBefore := range 6 {
		r := daemontest.NewRig(t)
		receipt := dispatch(t, r, startRun("c1"))
		for range stepsBefore {
			if err := step(r); err != nil {
				t.Fatal(err)
			}
		}
		r.Restart(t)
		r.Settle(t)
		run, lead := leadOf(r, receipt)
		if run.Status != core.RunRunning || lead.Session != 1 || lead.Status.Over() {
			t.Fatalf("after a restart %d steps in, run %s and lead %+v: want a running run on the lead's first session", stepsBefore, run.Status, lead)
		}
		if n := len(r.Sidecars.Sessions()); n != 1 {
			t.Fatalf("restart after %d steps: %d sessions launched, want 1", stepsBefore, n)
		}
	}
}

// The same holds for a subagent spawned just before the restart: it is launched and not written
// off, and the lead is not told it was lost.
func TestARestartLosesNoSubagentWhoseLaunchIsPending(t *testing.T) {
	r := daemontest.NewRig(t)
	run, lead := beginRun(t, r)
	token := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Credentials.GetToken()
	resp, err := r.AgentService.Dispatch(agentContext(lead.ID, 1, token), &pb.DispatchRequest{Command: &pb.Command{Id: "spawn", Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
		RunId: string(run.ID), Role: pb.AgentRole_AGENT_ROLE_WORKER, Brief: "work",
	}}}})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("spawn: %v, %v", resp, err)
	}
	r.Restart(t)
	r.Settle(t)
	for _, a := range r.Daemon.State().Agents(run.ID) {
		if a.Status == core.AgentLost || a.Status == core.AgentFailed {
			t.Fatalf("agent %+v was written off by a restart", a)
		}
	}
	if n := len(r.Sidecars.Sessions()); n != 2 {
		t.Fatalf("%d sessions launched, want the lead's and the worker's", n)
	}
}

// Finding 2. The operator's socket takes no agent credentials, the agent socket takes nothing
// without them, and a session's environment names the agent socket and nothing else.
func TestEachSocketServesOnlyItsOwnCallers(t *testing.T) {
	r := daemontest.NewRig(t)
	run, lead := beginRun(t, r)
	session := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})
	ctx := agentContext(lead.ID, 1, session.Credentials.GetToken())
	stop := &pb.DispatchRequest{Command: &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}}}

	if _, err := r.Service.Dispatch(ctx, stop); status.Code(err) != codes.PermissionDenied {
		t.Errorf("the operator socket took an agent's credentials: %v", err)
	}
	if _, err := r.AgentService.Dispatch(context.Background(), stop); status.Code(err) != codes.Unauthenticated {
		t.Errorf("the agent socket took a call with no credentials: %v", err)
	}
	if got := run.ID; r.Daemon.State().Runs()[0].Status != core.RunRunning || got == "" {
		t.Errorf("a refused call changed the run")
	}
	if got := session.Credentials.GetEndpoint(); got != daemontest.AgentSocket {
		t.Errorf("the tool server is told the socket %q, want the agent socket %q", got, daemontest.AgentSocket)
	}
	env, err := envcodec.Encode(session.Credentials)
	if err != nil {
		t.Fatal(err)
	}
	for name, value := range env.Set {
		if value != daemontest.AgentSocket && strings.Contains(value, ".sock") {
			t.Errorf("%s=%q names a socket besides the agent's", name, value)
		}
	}
}

// Finding 3. A call to the world that never returns costs one bound and is retried, whichever call
// it is, and never loses the session.
func TestACallThatHangsIsBoundedAndRetried(t *testing.T) {
	t.Run("launch", func(t *testing.T) {
		r := daemontest.NewRig(t)
		receipt := dispatch(t, r, startRun("c1"))
		r.Sidecars.Stall = 1
		if err := step(r); !errors.Is(err, daemon.ErrExpired) {
			t.Fatalf("a hanging launch gave %v, want ErrExpired", err)
		}
		r.Settle(t)
		if _, lead := leadOf(r, receipt); lead.Status.Over() || lead.Session != 1 {
			t.Fatalf("lead %+v, want live on its first session", lead)
		}
	})
	t.Run("send", func(t *testing.T) {
		r := daemontest.NewRig(t)
		_, lead := beginRun(t, r)
		dispatch(t, r, &pb.Command{Id: "tell", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "hi"}}})
		r.Sidecars.Stall = 1
		if err := step(r); !errors.Is(err, daemon.ErrExpired) {
			t.Fatalf("a hanging send gave %v, want ErrExpired", err)
		}
		r.Settle(t)
		if got := command(r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})); len(got) < 2 {
			t.Fatalf("commands %v: the message was never delivered after the bound ended", got)
		}
	})
	t.Run("attach", func(t *testing.T) {
		r := daemontest.NewRig(t)
		_, lead := beginRun(t, r)
		r.Sidecars.Stall = 1
		r.Restart(t)
		if !errors.Is(r.ReattachErr, daemon.ErrExpired) {
			t.Fatalf("a hanging attach reported %v, want ErrExpired", r.ReattachErr)
		}
		r.Settle(t)
		if got, _ := r.Daemon.State().Agent(lead.ID); got.Status.Over() || got.Session != 1 {
			t.Fatalf("lead %+v: a slow sidecar was taken for a dead one", got)
		}
	})
	t.Run("jobs", func(t *testing.T) {
		r := daemontest.NewRig(t)
		beginRun(t, r)
		r.Clock.Advance(daemontest.PollEvery)
		r.JobStall = 1
		if err := step(r); !errors.Is(err, daemon.ErrExpired) {
			t.Fatalf("a hanging job poll gave %v, want ErrExpired", err)
		}
	})
	t.Run("a daemon without bounds is refused", func(t *testing.T) {
		if _, err := daemon.Open(daemon.Config{}); err == nil {
			t.Fatal("Open accepted a config with no Bounds")
		}
	})
}

// Finding 4. The reason a launch failed reaches the agent's record, the lead, and so the client.
func TestALaunchErrorReachesTheRecord(t *testing.T) {
	r := daemontest.NewRig(t)
	run, lead := beginRun(t, r)
	token := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Credentials.GetToken()
	r.Sidecars.LaunchFailures, r.Sidecars.LaunchError = 1, errors.New("no free gpu on this host")
	resp, err := r.AgentService.Dispatch(agentContext(lead.ID, 1, token), &pb.DispatchRequest{Command: &pb.Command{Id: "spawn", Body: &pb.Command_SpawnAgent{SpawnAgent: &pb.SpawnAgent{
		RunId: string(run.ID), Role: pb.AgentRole_AGENT_ROLE_WORKER, Brief: "work",
	}}}})
	if err != nil {
		t.Fatal(err)
	}
	r.Settle(t)
	worker, _ := r.Daemon.State().Agent(core.AgentID(resp.GetReceipt().GetAgentId()))
	if worker.Status != core.AgentLost || !strings.Contains(worker.EndDetail, "no free gpu on this host") {
		t.Fatalf("worker %+v: want it lost, with the launch error as the reason", worker)
	}
	var told bool
	records, err := r.Store.Messages(run.ID, 0, 100)
	if err != nil {
		t.Fatal(err)
	}
	for _, rec := range records {
		if s, ok := rec.Message.Body.(core.SubagentSettled); ok && s.Agent == worker.ID && strings.Contains(s.Detail, "no free gpu") {
			told = true
		}
	}
	if !told {
		t.Fatal("the lead was not told why its subagent never started")
	}
	if got := agentOnWire(t, r, worker.ID).GetEndDetail(); !strings.Contains(got, "no free gpu") {
		t.Fatalf("the wire agent carries detail %q", got)
	}
}

func agentOnWire(t *testing.T, r *daemontest.Rig, id core.AgentID) *pb.Agent {
	t.Helper()
	w, first, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	for _, m := range first {
		for _, a := range client.WatchRunsSnapshot(m).GetAgents() {
			if a.GetId() == string(id) {
				return a
			}
		}
	}
	t.Fatalf("agent %s is not in the snapshot", id)
	return nil
}

// A daemon with no job source asks none, and says nothing of jobs.
func TestADaemonWithNoJobSourceStillSteps(t *testing.T) {
	r := daemontest.NewRig(t)
	d, err := daemon.Open(daemon.Config{
		Engine:   daemon.EngineDeps{Store: r.Faults, Clock: r.Clock, Nonces: r.Nonces, Hub: hub.New(r.Limits)},
		Sidecars: r.Sidecars, Authority: r.Auth, Bounds: r.Bounds, AgentSocket: daemontest.AgentSocket,
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := d.Step(context.Background()); err != nil {
		t.Fatalf("a step with no job source failed: %v", err)
	}
}

// Finding 5. A store that fails to settle its rows must not strand effects: they are settled by a
// later step of the same daemon, with no restart.
func TestFailedSettlingStrandsNoEffect(t *testing.T) {
	r := daemontest.NewRig(t)
	_, lead := beginRun(t, r)
	dispatch(t, r, &pb.Command{Id: "tell", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "hi"}}})
	r.Sidecars.Stall, r.Faults.FailRelease = 1, 1
	if err := step(r); err == nil {
		t.Fatal("a stalled send and a failed release reported no error")
	}
	r.Settle(t)
	if got := command(r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})); len(got) < 2 {
		t.Fatalf("commands %v: the message was stranded by a failed release", got)
	}
}

func TestFailedCompleteIsRetriedNotRepeated(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	r.Faults.FailComplete = 1
	if err := step(r); !errors.Is(err, daemontest.ErrInjected) {
		t.Fatalf("a failed complete gave %v", err)
	}
	r.Settle(t)
	_, lead := leadOf(r, receipt)
	if got := command(r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})); len(got) != 1 {
		t.Fatalf("commands %v, want the first turn once", got)
	}
}

// Finding 6. An effect that fails holds back the later effects of its session, so a stop never
// reaches a session before the turn it stops.
func TestAFailedEffectHoldsBackTheLaterEffectsOfItsSession(t *testing.T) {
	r := daemontest.NewRig(t)
	run, lead := beginRun(t, r)
	r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1}).Held = true // the turn stays open.
	dispatch(t, r, &pb.Command{Id: "tell", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "hi"}}})
	dispatch(t, r, &pb.Command{Id: "stop", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}})
	r.Sidecars.Stall = 1
	session := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})
	before := len(command(session))
	if err := step(r); !errors.Is(err, daemon.ErrExpired) {
		t.Fatalf("got %v, want the first effect to run out of time", err)
	}
	if got := command(session)[before:]; len(got) != 0 {
		t.Fatalf("%v reached the session after its first effect failed", got)
	}
	r.Settle(t)
	got := command(session)[before:]
	if len(got) < 2 || got[0] != "start" {
		t.Fatalf("commands %v: want the turn to start before anything that stops it", got)
	}
}

// A stream that drops and is reopened after the committed offset replays the deltas since it: they
// are shown once.
func TestAReopenedStreamDoesNotShowItsDeltasTwice(t *testing.T) {
	r := daemontest.NewRig(t)
	_, lead := beginRun(t, r)
	session := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})
	w, _, err := r.Service.WatchAgentFrom(lead.ID, 10, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	session.EmitEvents(&pb.SessionEvent{Body: &pb.SessionEvent_Delta{Delta: &pb.SessionDelta{ItemId: "item-live", Body: &pb.SessionDelta_Text{Text: "abc"}}}})
	r.Settle(t)
	session.BreakStream()
	_ = step(r)
	r.Settle(t)
	r.Hub.Flush()
	msgs, err := w.Poll()
	if err != nil {
		t.Fatal(err)
	}
	var text string
	for _, m := range msgs {
		text += m.GetDelta().GetText()
	}
	if text != "abc" {
		t.Fatalf("the watcher saw the delta as %q, want %q once", text, "abc")
	}
}
