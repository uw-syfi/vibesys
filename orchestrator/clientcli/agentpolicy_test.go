package clientcli_test

import (
	"context"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// An agent that was killed or lost before its provider reported has no usage, and its cost is not in
// any total. The summary says how many agents are in that position, so the total is not read as the
// whole cost; an agent that reported before it died is not counted, and one that never ran a turn is
// not called unmeasured.
func TestGetSaysHowManyAgentsEndedWithoutAMeasuredCost(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	run.Usage = &pb.Usage{CostUsd: ptr(0.5)}
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)
	lead.Usage = &pb.Usage{CostUsd: ptr(0.5)}
	killed := agentOf("run-1", "judge-1", pb.AgentRole_AGENT_ROLE_JUDGE, pb.AgentStatus_AGENT_STATUS_LOST, 1)
	failed := agentOf("run-1", "judge-2", pb.AgentRole_AGENT_ROLE_JUDGE, pb.AgentStatus_AGENT_STATUS_FAILED, 2)
	measured := agentOf("run-1", "judge-3", pb.AgentRole_AGENT_ROLE_JUDGE, pb.AgentStatus_AGENT_STATUS_LOST, 3)
	measured.Usage = &pb.Usage{OutputTokens: ptr(uint64(10))}
	neverRan := agentOf("run-1", "judge-4", pb.AgentRole_AGENT_ROLE_JUDGE, pb.AgentStatus_AGENT_STATUS_FAILED, 4)
	turn := func(id, agent string) *pb.Turn {
		return &pb.Turn{Id: id, AgentId: agent, Ordinal: 1, Status: pb.TurnStatus_TURN_STATUS_FAILED, StartedAt: at(1)}
	}
	frame := &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: &pb.RunsSnapshot{
		Runs: []*pb.Run{run}, Agents: []*pb.Agent{lead, killed, failed, measured, neverRan}, Sequence: 5,
		Turns: []*pb.Turn{turn("t1", "lead-run-1"), turn("t2", "judge-1"), turn("t3", "judge-2"), turn("t4", "judge-3")},
	}}}

	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.run(context.Background(), "run", "get", "run-1")
	contains(t, "summary", got.stdout,
		"usage       cost $0.5000; 2 agents ended without a measured cost, so this is not the whole cost",
		"judge-1  ended without a measured cost",
		"judge-2  ended without a measured cost")
	if strings.Contains(got.stdout, "judge-4") {
		t.Errorf("an agent that never ran a turn is called unmeasured: %s", got.stdout)
	}
	if strings.Contains(got.stdout, "judge-3  ended without") {
		t.Errorf("an agent that reported tokens is called unmeasured: %s", got.stdout)
	}

	// A run whose agents all reported says nothing about missing cost.
	whole := snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{lead})
	w = newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{whole}}))
	if got := w.run(context.Background(), "run", "get", "run-1"); strings.Contains(got.stdout, "without a measured cost") {
		t.Errorf("a fully measured run is called incomplete: %s", got.stdout)
	}
}

// run get shows what the run may use, so an operator sees the allowance before a spawn is refused.
func TestGetShowsWhatTheRunMayUse(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	run.AllowedAgents = []*pb.AllowedAgent{
		{Harness: pb.Harness_HARNESS_CODEX, Model: "gpt-m"}, {Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}
	frame := snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	contains(t, "summary", w.run(context.Background(), "run", "get", "run-1").stdout, "may use     codex gpt-m; claude code (default model)")
}

// --allow-agent takes a harness, with a model after the first colon, and any number of times; a
// harness that does not exist is a usage error naming the value.
func TestStartCarriesEveryAllowAgentAndRefusesAnUnknownHarness(t *testing.T) {
	f := &fakeService{dispatch: func(cmd *pb.Command) (*pb.DispatchResponse, error) {
		return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_RUN_NOT_FOUND}}, nil
	}}
	w := newWorld(t, serveFake(t, f))
	w.run(context.Background(), "run", "start", "--task", "t", "--harness", "codex", "--model", "gpt-m",
		"--allow-agent", "claude_code:claude-sonnet", "--allow-agent", "codex:gpt:odd", "--allow-agent", "claude_code")
	sent := f.sent()
	if len(sent) != 1 {
		t.Fatalf("sent %d commands", len(sent))
	}
	var got []string
	for _, a := range sent[0].GetStartRun().GetAllowAgents() {
		got = append(got, a.GetHarness().String()+"/"+a.GetModel())
	}
	want := "HARNESS_CLAUDE_CODE/claude-sonnet HARNESS_CODEX/gpt:odd HARNESS_CLAUDE_CODE/"
	if strings.Join(got, " ") != want {
		t.Fatalf("the start carries %v, want %s", got, want)
	}

	bad := w.run(context.Background(), "run", "start", "--task", "t", "--allow-agent", "vim:x")
	if bad.code != 2 || !strings.Contains(bad.stderr, `"vim:x"`) {
		t.Fatalf("an unknown harness: exit %d, stderr %q", bad.code, bad.stderr)
	}
}
