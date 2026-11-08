package daemon_test

import (
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// declareAs sends declare_task's command as the run's lead, over the agent listener.
func declareAs(t *testing.T, r *daemontest.Rig, run core.RunID, id, task string) *pb.Receipt {
	t.Helper()
	lead := r.Daemon.State().Agents(run)[0]
	ctx := agentContext(lead.ID, lead.Session, r.Auth.Token(core.Session{Agent: lead.ID, N: lead.Session}))
	resp, err := r.AgentService.Dispatch(ctx, &pb.DispatchRequest{Command: &pb.Command{Id: id, Body: &pb.Command_DeclareTask{
		DeclareTask: &pb.DeclareTask{RunId: string(run), Task: task}}}})
	if err != nil {
		t.Fatalf("declare %s: %v", task, err)
	}
	return resp.GetReceipt()
}

// The setup lead names the task over the agent listener; the latest name wins, and the run's
// task stays empty until setup completes.
func TestTheSetupLeadDeclaresATaskThroughTheAgentListener(t *testing.T) {
	r := daemontest.NewRig(t)
	begin := dispatch(t, r, &pb.Command{Id: "begin", Body: &pb.Command_BeginSetup{BeginSetup: &pb.BeginSetup{
		Repository: "/repo", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}})
	r.Settle(t)
	run := core.RunID(begin.GetRunId())
	for i, name := range []string{"matmul", "matmul-v2", "matmul-v2"} {
		if rec := declareAs(t, r, run, "declare-"+string(rune('a'+i)), name); rec.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
			t.Fatalf("declare %q refused: %v", name, rec)
		}
		got, _ := r.Daemon.State().Run(run)
		if got.SetupTask != name || got.Task != "" {
			t.Fatalf("after declaring %q: setup task %q, task %q", name, got.SetupTask, got.Task)
		}
	}
}

// A run that is not in setup refuses the tool's command as NOT_IN_SETUP and keeps its task.
func TestAnOptimizingLeadCannotDeclareATask(t *testing.T) {
	r := daemontest.NewRig(t)
	start := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	run := core.RunID(start.GetRunId())
	if rec := declareAs(t, r, run, "declare", "other"); rec.GetRefusal() != pb.Refusal_REFUSAL_NOT_IN_SETUP {
		t.Fatalf("receipt %v, want NOT_IN_SETUP", rec)
	}
	if got, _ := r.Daemon.State().Run(run); got.Task != "task" || got.SetupTask != "" {
		t.Fatalf("task %q, setup task %q", got.Task, got.SetupTask)
	}
}
