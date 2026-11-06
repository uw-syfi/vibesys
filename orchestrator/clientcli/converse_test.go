package clientcli_test

import (
	"fmt"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// A lead that asked a planner for a plan, was handed it, and asked a worker (started by the planner, as the
// planner's helper) to build it. The run has ended, so its views are replays and are exact.
func conversation(status pb.RunStatus, tweaks ...func(run *pb.Run, agents []*pb.Agent)) *fakeService {
	run := runOf("run-1", "/work/repo", status, 0)
	if status == pb.RunStatus_RUN_STATUS_ENDED {
		run.StopReason, run.EndedAt = pb.RunStopReason_RUN_STOP_REASON_OPERATOR, at(20)
	}
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ENDED)
	lead.Usage = &pb.Usage{CostUsd: ptr(0.0733)}
	lead.EndedAt = at(20)
	planner := agentOf("run-1", "agent-2ae74088c0de", pb.AgentRole_AGENT_ROLE_PLANNER, pb.AgentStatus_AGENT_STATUS_ENDED, 1)
	planner.ParentAgentId = lead.Id
	planner.EndedAt = at(5)
	worker := agentOf("run-1", "agent-9f01b7d4e5a2", pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentStatus_AGENT_STATUS_ENDED, 6)
	worker.ParentAgentId = lead.Id
	worker.PlanNumber = 1
	worker.EndedAt = at(12)
	handoff := &pb.Message{Id: "m1", RunId: "run-1", FromAgentId: planner.Id, ToAgentId: lead.Id, Delivery: pb.Delivery_DELIVERY_CONSUMED, At: at(5),
		Body: &pb.Message_Handoff{Handoff: &pb.Handoff{Claim: "plan 1: cache the tokenizer's merge table", Body: &pb.Handoff_Planner{Planner: &pb.PlannerHandoff{Plan: "plans/plan-001.md"}}}}}
	done := &pb.Message{Id: "m2", RunId: "run-1", FromAgentId: worker.Id, ToAgentId: lead.Id, Delivery: pb.Delivery_DELIVERY_CONSUMED, At: at(12),
		Body: &pb.Message_Handoff{Handoff: &pb.Handoff{Claim: "plan 1 is 9% faster", Body: &pb.Handoff_Worker{Worker: &pb.WorkerHandoff{Outcome: &pb.WorkerHandoff_NoCandidate{NoCandidate: &pb.NoCandidate{Blocker: "none"}}}}}}}
	turns := []*pb.Turn{
		{Id: "t1", AgentId: lead.Id, Ordinal: 1, Trigger: pb.TurnTrigger_TURN_TRIGGER_BRIEF, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(0), EndedAt: at(1),
			Usage: &pb.Usage{CostUsd: ptr(0.0421), InputTokens: ptr(uint64(2_400_000)), OutputTokens: ptr(uint64(1200))}},
		{Id: "t2", AgentId: lead.Id, Ordinal: 2, Trigger: pb.TurnTrigger_TURN_TRIGGER_HANDOFF, TriggerMessageId: "m1", Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(5), EndedAt: at(6),
			Usage: &pb.Usage{CostUsd: ptr(0.0312), OutputTokens: ptr(uint64(800))}},
		{Id: "t3", AgentId: planner.Id, Ordinal: 1, Trigger: pb.TurnTrigger_TURN_TRIGGER_BRIEF, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(2), EndedAt: at(4), Usage: &pb.Usage{OutputTokens: ptr(uint64(2100))}},
		{Id: "t4", AgentId: worker.Id, Ordinal: 1, Trigger: pb.TurnTrigger_TURN_TRIGGER_BRIEF, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(7), EndedAt: at(12), Usage: &pb.Usage{OutputTokens: ptr(uint64(5400))}},
	}
	read := func(path string) *pb.ToolCall {
		return &pb.ToolCall{Call: &pb.ToolCall_Read{Read: &pb.ReadCall{Target: path}}}
	}
	exit1 := int32(1)
	failing := &pb.ToolCall{Call: &pb.ToolCall_Shell{Shell: &pb.ShellCall{Command: "pytest -x tests/"}}, ExitCode: &exit1, IsError: true, Output: "collected 3 items\n\nFAILED tests/test_tok.py::test_merge - KeyError: 'ab'\n"}
	prompt := func(agent string, ord uint32, turn, text string) *pb.Item {
		return &pb.Item{Id: fmt.Sprintf("%s-%d", agent, ord), TurnId: turn, AgentId: agent, Ordinal: ord, Status: pb.ItemStatus_ITEM_STATUS_FINISHED, At: at(int(ord)), Body: &pb.Item_Prompt{Prompt: &pb.Text{Text: text}}}
	}
	for _, tweak := range tweaks {
		tweak(run, []*pb.Agent{lead, planner, worker})
	}
	snapshot := &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: &pb.RunsSnapshot{
		Runs: []*pb.Run{run}, Agents: []*pb.Agent{lead, planner, worker}, Turns: turns, Messages: []*pb.Message{handoff, done}, Sequence: 30}}}
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshot}, ended: make(chan string, 64), items: map[string][]*pb.Item{
		lead.Id: {
			prompt(lead.Id, 1, "t1", "Make the tokenizer benchmark faster.\nKeep the outputs identical."),
			assistantItem(lead.Id, 2, "t1", "I will look at the repo, then ask a planner for a plan.\n\nFirst the benchmark itself."),
			toolItem(lead.Id, 3, "t1", read("bench/tokenize.py")),
			toolItem(lead.Id, 4, "t1", shell("git log --oneline | head -5\ngit status")),
			toolItem(lead.Id, 5, "t1", failing),
			assistantItem(lead.Id, 6, "t1", "The baseline test fails on a clean tree; a planner can work around it."),
			assistantItem(lead.Id, 7, "t2", "The planner proposes plan 1. A worker will build it."),
			toolItem(lead.Id, 8, "t2", &pb.ToolCall{Call: &pb.ToolCall_Edit{Edit: &pb.EditCall{Targets: []string{"state/plans.md", "state/notes.md"}}}}),
		},
		planner.Id: {
			prompt(planner.Id, 1, "t3", "Propose one plan for the tokenizer."),
			toolItem(planner.Id, 2, "t3", &pb.ToolCall{Call: &pb.ToolCall_Search{Search: &pb.SearchCall{Pattern: "merge_table"}}}),
			toolItem(planner.Id, 3, "t3", &pb.ToolCall{Call: &pb.ToolCall_Write{Write: &pb.WriteCall{Target: "plans/plan-001.md"}}}),
			assistantItem(planner.Id, 4, "t3", "Plan 1 caches the merge table between calls."),
		},
		worker.Id: {
			prompt(worker.Id, 1, "t4", "Implement plan 1."),
			toolItem(worker.Id, 2, "t4", &pb.ToolCall{Call: &pb.ToolCall_Other{Other: &pb.OtherCall{Name: "TodoWrite", Text: "1 item\nmore"}}}),
			toolItem(worker.Id, 3, "t4", shell("python bench/tokenize.py --check")),
			assistantItem(worker.Id, 4, "t4", "Done: 9% faster, outputs identical."),
		},
	}}
	// An item happened inside its turn, a second apart from the one before.
	started := map[string]time.Time{}
	for _, t := range turns {
		started[t.Id] = t.StartedAt.AsTime()
	}
	for _, items := range f.items {
		for _, it := range items {
			it.At = timestamppb.New(started[it.TurnId].Add(time.Duration(it.Ordinal) * time.Second))
		}
	}
	return f
}
