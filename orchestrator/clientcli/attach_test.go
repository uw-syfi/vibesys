package clientcli_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// goldenScript is a run from its snapshot to its end, covering every kind of line attach shows.
func goldenScript() []*pb.WatchRunsResponse {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_WAITING)
	lead.Model = "claude-sonnet"
	worker := agentOf("run-1", "worker-1", pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentStatus_AGENT_STATUS_ENDED, 1)
	worker.EndedAt = at(4)
	handoff := &pb.Message{Id: "m1", RunId: "run-1", FromAgentId: "worker-1", ToAgentId: "lead-run-1", Delivery: pb.Delivery_DELIVERY_CONSUMED, At: at(4),
		Body: &pb.Message_Handoff{Handoff: &pb.Handoff{PlanNumber: 2, Claim: "plan 2 is 3%\nfaster", Commit: "abc"}}}
	snapshot := &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: &pb.RunsSnapshot{
		Runs: []*pb.Run{run}, Agents: []*pb.Agent{lead, worker}, Messages: []*pb.Message{handoff}, Sequence: 10,
		Turns: []*pb.Turn{
			{Id: "t1", AgentId: "lead-run-1", Ordinal: 1, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(0), EndedAt: at(2), Usage: &pb.Usage{CostUsd: ptr(0.0421)}},
			{Id: "t2", AgentId: "worker-1", Ordinal: 1, Status: pb.TurnStatus_TURN_STATUS_COMPLETED, StartedAt: at(1), EndedAt: at(4)},
		},
	}}}
	failing := agentOf("run-1", "worker-2", pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentStatus_AGENT_STATUS_LOST, 5)
	failing.EndedAt, failing.EndDetail = at(7), "the sidecar went away\nwhile  running"
	settled := &pb.Message{Id: "m3", RunId: "run-1", FromAgentId: "", ToAgentId: "lead-run-1", Delivery: pb.Delivery_DELIVERY_QUEUED, At: at(7),
		Body: &pb.Message_SubagentSettled{SubagentSettled: &pb.SubagentSettled{AgentId: "worker-2", Status: pb.AgentStatus_AGENT_STATUS_LOST, Detail: "lost"}}}
	jobDone := &pb.Message{Id: "m4", RunId: "run-1", ToAgentId: "lead-run-1", Delivery: pb.Delivery_DELIVERY_QUEUED, At: at(8),
		Body: &pb.Message_JobFinished{JobFinished: &pb.JobFinished{JobId: "job-9", Outcome: pb.JobOutcome_JOB_OUTCOME_FAILED, Detail: "exit 2"}}}
	rejected := &pb.Message{Id: "m5", RunId: "run-1", ToAgentId: "worker-2", Delivery: pb.Delivery_DELIVERY_REJECTED, Rejection: pb.Rejection_REJECTION_AGENT_GONE, At: at(9),
		Body: &pb.Message_Text{Text: "hello"}}
	stopping := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_STOPPING, 0)
	stopping.StopReason = pb.RunStopReason_RUN_STOP_REASON_OPERATOR
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	ended.StopReason, ended.EndedAt = pb.RunStopReason_RUN_STOP_REASON_OPERATOR, at(12)
	return []*pb.WatchRunsResponse{
		snapshot,
		agentEvent(11, failing, 5),
		messageEvent(12, settled, 7),
		eventFrame(13, "run-1", 8, func(ev *pb.Event) {
			ev.Body = &pb.Event_TurnUpdated{TurnUpdated: &pb.Turn{Id: "t3", AgentId: "lead-run-1", Ordinal: 2, Status: pb.TurnStatus_TURN_STATUS_FAILED, StartedAt: at(7), EndedAt: at(8)}}
		}),
		messageEvent(14, jobDone, 8),
		searchFrame("run-1", 1, jobOf("job-1", cliv1.JobState_JOB_STATE_RUNNING, true)),
		searchFrame("run-1", 2, jobOf("job-1", cliv1.JobState_JOB_STATE_SUCCEEDED, true)),
		messageEvent(15, reportMessage("run-1", "m6", "plan 2 won:  3% faster", 9), 9),
		messageEvent(16, rejected, 9),
		runEvent(17, stopping, 10),
		runEvent(18, ended, 12),
	}
}

func ptr[T any](v T) *T { return &v }

func jobOf(id string, state cliv1.JobState, measure bool) *cliv1.Job {
	job := &cliv1.Job{Id: id, State: state, SubmittedAt: at(6)}
	if state == cliv1.JobState_JOB_STATE_SUCCEEDED {
		job.FinishedAt = at(9)
	}
	if measure {
		job.Spec = &cliv1.JobSpec{Settings: &cliv1.JobSpec_Measure{Measure: &cliv1.MeasureSettings{Samples: 3}}}
	}
	return job
}

func searchFrame(run string, revision uint64, jobs ...*cliv1.Job) *pb.WatchRunsResponse {
	state := &pb.SearchState{RunId: run, Revision: revision, Health: &pb.SearchHealth{State: &pb.SearchHealth_LiveSince{LiveSince: timestamppb.New(epoch)}}, Jobs: jobs}
	if revision >= 2 {
		state.Results = []*cliv1.Result{{JobId: jobs[0].GetId(), Commit: "0123456789abcdef", RecordedAt: at(9),
			Outcome: &cliv1.Result_Measure{Measure: &cliv1.MeasureResult{Metric: &cliv1.Metric{Name: "score"}, Value: 12.5}}}}
	}
	return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Search{Search: state}}
}

// Attach renders typed messages as lines, and the same messages always render the same lines.
func TestAttachRendersTheRunAsReadableLines(t *testing.T) {
	w := newWorld(t, serveFake(t, &fakeService{frames: goldenScript()}))
	got := w.run(context.Background(), "run", "attach", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	golden := filepath.Join("testdata", "attach.golden")
	if os.Getenv("CLIENTCLI_UPDATE_GOLDEN") != "" {
		if err := os.WriteFile(golden, []byte(got.stdout), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(golden)
	if err != nil {
		t.Fatal(err)
	}
	if got.stdout != string(want) {
		t.Errorf("attach output differs from %s (CLIENTCLI_UPDATE_GOLDEN=1 rewrites it)\n--- got\n%s--- want\n%s", golden, got.stdout, want)
	}
	contains(t, "stderr", got.stderr, "Ctrl-C detaches")
}

func TestAttachExitsOneForARunThatFailed(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_FAILED, 0)
	run.StopReason = pb.RunStopReason_RUN_STOP_REASON_LEAD_GONE
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_FAILED)
	lead.EndDetail = "the agent image is missing"
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{lead})}}))
	got := w.run(context.Background(), "run", "attach", "run-1")
	if got.code != 1 {
		t.Fatalf("exit %d, want 1", got.code)
	}
	contains(t, "stdout", got.stdout, "run failed: the agent image is missing")
}

// Ctrl-C, a cancelled context, only detaches: no command goes to the service, the run is still
// going, and the caller is told how to come back or end it.
func TestAttachCtrlCDetachesWithoutSendingAnything(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)})}}
	w := newWorld(t, serveFake(t, f))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	w.onStdout = func(written string) {
		if strings.Contains(written, "lead lead-run-1 started") {
			cancel()
		}
	}
	got := w.runUnbounded(ctx, "--root", "/some/root", "run", "attach")
	if got.code != 0 {
		t.Fatalf("exit %d, want 0; stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "run run-1 is still going",
		"vibesys-client --root /some/root run attach run-1", "vibesys-client --root /some/root run stop run-1")
	if sent := f.sent(); len(sent) != 0 {
		t.Errorf("Ctrl-C sent %v", sent)
	}
}
