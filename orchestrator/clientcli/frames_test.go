package clientcli_test

import (
	"fmt"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

var epoch = time.Date(2026, 1, 2, 3, 0, 0, 0, time.UTC)

func at(minutes int) *timestamppb.Timestamp {
	return timestamppb.New(epoch.Add(time.Duration(minutes) * time.Minute))
}

func runOf(id, repo string, status pb.RunStatus, minutes int) *pb.Run {
	return &pb.Run{Id: id, Repository: repo, Task: "toy", Status: status, LeadAgentId: "lead-" + id, FanoutLimit: 2, CreatedAt: at(minutes)}
}

func agentOf(run, id string, role pb.AgentRole, status pb.AgentStatus, minutes int) *pb.Agent {
	return &pb.Agent{
		Id: id, RunId: run, Role: role, Harness: pb.Harness_HARNESS_CLAUDE_CODE, Status: status, StartedAt: at(minutes), Session: 1,
		StopStage: pb.AgentStopStage_AGENT_STOP_STAGE_NONE, Capabilities: &pb.Capabilities{},
	}
}

func leadOf(run string, status pb.AgentStatus) *pb.Agent {
	return agentOf(run, "lead-"+run, pb.AgentRole_AGENT_ROLE_LEAD, status, 0)
}

func snapshotFrame(seq uint64, truncated bool, runs []*pb.Run, agents []*pb.Agent, messages ...*pb.Message) *pb.WatchRunsResponse {
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: &pb.RunsSnapshot{
		Runs: runs, Agents: agents, Messages: messages, Sequence: seq, Truncated: truncated,
	}}}
}

func eventFrame(seq uint64, run string, minutes int, body func(*pb.Event)) *pb.WatchRunsResponse {
	ev := &pb.Event{Sequence: seq, RunId: run, At: at(minutes)}
	body(ev)
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Event{Event: ev}}
}

func runEvent(seq uint64, r *pb.Run, minutes int) *pb.WatchRunsResponse {
	return eventFrame(seq, r.GetId(), minutes, func(ev *pb.Event) { ev.Body = &pb.Event_RunUpdated{RunUpdated: r} })
}

func agentEvent(seq uint64, a *pb.Agent, minutes int) *pb.WatchRunsResponse {
	return eventFrame(seq, a.GetRunId(), minutes, func(ev *pb.Event) { ev.Body = &pb.Event_AgentUpdated{AgentUpdated: a} })
}

func messageEvent(seq uint64, m *pb.Message, minutes int) *pb.WatchRunsResponse {
	return eventFrame(seq, m.GetRunId(), minutes, func(ev *pb.Event) { ev.Body = &pb.Event_MessageUpdated{MessageUpdated: m} })
}

func reportMessage(run, id, headline string, minutes int) *pb.Message {
	return &pb.Message{
		Id: id, RunId: run, FromAgentId: "lead-" + run, Delivery: pb.Delivery_DELIVERY_CONSUMED, At: at(minutes),
		Body: &pb.Message_Report{Report: &pb.Report{Headline: headline}},
	}
}

func runIDs(n int) []string {
	ids := make([]string, n)
	for i := range ids {
		ids[i] = fmt.Sprintf("run-%02d", i)
	}
	return ids
}
