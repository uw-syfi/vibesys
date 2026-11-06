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

// snapshotWithSearch is a snapshot frame that also carries the search view of its run.
func snapshotWithSearch(seq uint64, runs []*pb.Run, agents []*pb.Agent, search *pb.SearchState) *pb.WatchRunsResponse {
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: &pb.RunsSnapshot{
		Runs: runs, Agents: agents, Searches: []*pb.SearchState{search}, Sequence: seq,
	}}}
}

func eventFrame(seq uint64, run string, minutes int, body func(*pb.RunChange)) *pb.WatchRunsResponse {
	ev := &pb.RunChange{Sequence: seq, RunId: run, At: at(minutes)}
	body(ev)
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Change{Change: ev}}
}

func runEvent(seq uint64, r *pb.Run, minutes int) *pb.WatchRunsResponse {
	return eventFrame(seq, r.GetId(), minutes, func(ev *pb.RunChange) { ev.Body = &pb.RunChange_Run{Run: r} })
}

func agentEvent(seq uint64, a *pb.Agent, minutes int) *pb.WatchRunsResponse {
	return eventFrame(seq, a.GetRunId(), minutes, func(ev *pb.RunChange) { ev.Body = &pb.RunChange_Agent{Agent: a} })
}

func messageEvent(seq uint64, m *pb.Message, minutes int) *pb.WatchRunsResponse {
	return eventFrame(seq, m.GetRunId(), minutes, func(ev *pb.RunChange) { ev.Body = &pb.RunChange_Message{Message: m} })
}

// reportMessage is the first progress report of a run.
func reportMessage(run, id, headline string, minutes int) *pb.Message {
	return reportOf(run, id, headline, minutes, 1, pb.ReportKind_REPORT_KIND_PROGRESS)
}

// reportOf is a report of a kind, numbered within its run.
func reportOf(run, id, headline string, minutes int, sequence uint32, kind pb.ReportKind) *pb.Message {
	return &pb.Message{
		Id: id, RunId: run, FromAgentId: "lead-" + run, Delivery: pb.Delivery_DELIVERY_CONSUMED, At: at(minutes),
		Body: &pb.Message_Report{Report: &pb.Report{Kind: kind, Sequence: sequence, Headline: headline}},
	}
}

// reported is a running run whose lead has posted this many reports.
func reported(run string, posted uint32) *pb.Run {
	r := runOf(run, "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	r.ReportsPosted = posted
	return r
}

// stalledRun is a running run that has stood still since the given minute.
func stalledRun(run string, since int) *pb.Run {
	r := runOf(run, "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	r.StalledSince = at(since)
	return r
}

func runIDs(n int) []string {
	ids := make([]string, n)
	for i := range ids {
		ids[i] = fmt.Sprintf("run-%02d", i)
	}
	return ids
}
