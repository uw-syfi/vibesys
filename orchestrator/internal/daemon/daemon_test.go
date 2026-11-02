package daemon_test

import (
	"context"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

func startRun(id string) *pb.Command {
	return &pb.Command{Id: id, Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: "/repo", Task: "task", FanoutLimit: 3, Brief: "go",
		Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}}
}

func dispatch(tb testing.TB, r *daemontest.Rig, cmd *pb.Command) *pb.Receipt {
	tb.Helper()
	resp, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: cmd})
	if err != nil {
		tb.Fatalf("dispatch %s: %v", cmd.GetId(), err)
	}
	return resp.GetReceipt()
}

func TestStartingARunLaunchesItsLeadAndRunsItsFirstTurn(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	if receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED || receipt.GetRunId() == "" {
		t.Fatalf("receipt %v, want an accepted start with a run", receipt)
	}
	r.Settle(t)
	run, ok := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	if !ok || run.Status != core.RunRunning {
		t.Fatalf("run %v, want running", run)
	}
	lead, _ := r.Daemon.State().Agent(run.Lead)
	if lead.Status != core.AgentIdle || lead.Session != 1 {
		t.Fatalf("lead %+v, want idle on session 1", lead)
	}
	session := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})
	if session == nil {
		t.Fatal("the lead's sidecar was never launched")
	}
	if got := session.Env[daemon.EnvAgent]; got != string(lead.ID) {
		t.Fatalf("session environment names agent %q, want %q", got, lead.ID)
	}
	if !r.Auth.Valid(core.Session{Agent: lead.ID, N: 1}, session.Env[daemon.EnvToken]) {
		t.Fatal("the session's token does not verify")
	}
}

func TestARetryGetsTheSameReceiptAndAReusedIDIsAConflict(t *testing.T) {
	r := daemontest.NewRig(t)
	first := dispatch(t, r, startRun("c1"))
	again := dispatch(t, r, startRun("c1"))
	if first.GetRunId() != again.GetRunId() || first.GetCommandDigest() != again.GetCommandDigest() {
		t.Fatalf("a retry answered %v, want %v", again, first)
	}
	other := startRun("c1")
	other.GetStartRun().Task = "another"
	_, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: other})
	if status.Code(err) != codes.AlreadyExists {
		t.Fatalf("reusing an id for another command gave %v, want AlreadyExists", err)
	}
	if n := len(r.Daemon.State().Runs()); n != 1 {
		t.Fatalf("%d runs, want 1", n)
	}
}

func TestARestartKeepsTheRunAndTheSidecar(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	before := r.Daemon.State()
	r.Restart(t)
	r.Settle(t)
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	lead, _ := r.Daemon.State().Agent(run.Lead)
	want, _ := before.Agent(run.Lead)
	if lead.Session != want.Session || lead.Status != want.Status {
		t.Fatalf("after a restart the lead is %+v, want %+v", lead, want)
	}
	if n := len(r.Sidecars.Sessions()); n != 1 {
		t.Fatalf("%d sidecars, want the one that outlived the daemon", n)
	}
}

func TestALostSidecarAtStartIsReportedAndTheLeadResumes(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	r.Sidecars.Session(core.Session{Agent: run.Lead, N: 1}).Die()
	r.Restart(t)
	r.Settle(t)
	lead, _ := r.Daemon.State().Agent(run.Lead)
	if lead.Session != 2 || lead.Status.Over() {
		t.Fatalf("lead %+v, want a live second session after its sidecar died", lead)
	}
}

func TestAnAgentCannotActWithoutItsToken(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	ctx := agentContext(run.Lead, 1, "wrong")
	_, err := r.Service.Dispatch(ctx, &pb.DispatchRequest{Command: &pb.Command{Id: "x", Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run.ID)}}}})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("a wrong token gave %v, want Unauthenticated", err)
	}
}
