package clientcli_test

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

func TestListShowsEveryRunNewestFirstAndAsJSON(t *testing.T) {
	l := newLive(t, nil)
	first, _ := l.startRun(t, "/work/repo", "alpha")
	second, _ := l.startRun(t, "/other", "beta")
	w := newWorld(t, l.Listener.Dial)

	got := w.run(context.Background(), "run", "list")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "list", got.stdout, "ID", "STATUS", "REPOSITORY", first, second, "alpha", "/other")

	got = w.run(context.Background(), "run", "list", "--json")
	var listed pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(got.stdout), &listed); err != nil {
		t.Fatalf("stdout %q: %v", got.stdout, err)
	}
	if len(listed.GetRuns()) != 2 || len(listed.GetAgents()) != 0 {
		t.Errorf("list --json = %v", &listed)
	}
}

// `get --json` is the RunsSnapshot of one run: it reads back into the same message, holds only
// that run, and carries its agents, turns and messages.
func TestGetJSONIsTheSnapshotOfOneRun(t *testing.T) {
	l := newLive(t, nil)
	run, lead := l.startRun(t, "/work/repo", "alpha")
	l.startRun(t, "/other", "beta")
	l.postReport(t, lead, "all done")
	w := newWorld(t, l.Listener.Dial)

	got := w.run(context.Background(), "run", "get", run, "--json")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	var snapshot pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(got.stdout), &snapshot); err != nil {
		t.Fatalf("stdout %q: %v", got.stdout, err)
	}
	if len(snapshot.GetRuns()) != 1 || snapshot.GetRuns()[0].GetId() != run {
		t.Fatalf("runs = %v", snapshot.GetRuns())
	}
	for _, a := range snapshot.GetAgents() {
		if a.GetRunId() != run {
			t.Errorf("agent %v belongs to another run", a)
		}
	}
	if len(snapshot.GetAgents()) == 0 || len(snapshot.GetTurns()) == 0 || len(snapshot.GetMessages()) == 0 || snapshot.GetSequence() == 0 {
		t.Errorf("the snapshot is missing agents, turns, messages or its sequence: %v", &snapshot)
	}
	again, err := protojson.Marshal(&snapshot)
	if err != nil {
		t.Fatal(err)
	}
	var reread pb.RunsSnapshot
	if err := protojson.Unmarshal(again, &reread); err != nil || !proto.Equal(&reread, &snapshot) {
		t.Errorf("the JSON does not round trip: %v", err)
	}
}

func TestGetSummaryNamesStatusAgentsCostAndTheLastReport(t *testing.T) {
	l := newLive(t, nil)
	run, lead := l.startRun(t, "/work/repo", "alpha")
	l.postReport(t, lead, "plan 1 won")
	w := newWorld(t, l.Listener.Dial)

	got := w.run(context.Background(), "run", "get", run[:6])
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "summary", got.stdout, "run "+run, "status", "running", "task", "alpha", "agents", "turns", "checks", "measures", "best", "last report", "plan 1 won")
}

// A snapshot that was cut to fit says so, and omits the counts that would be wrong.
func TestGetOnATruncatedSnapshotSaysSoAndOmitsCounts(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, true, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})}}
	w := newWorld(t, serveFake(t, f))

	got := w.run(context.Background(), "run", "get", "run-1")
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "summary", got.stdout, "cut to fit")
	for _, counted := range []string{"agents", "turns", "checks", "measures"} {
		if strings.Contains(got.stdout, "\n  "+counted+" ") {
			t.Errorf("a truncated summary shows %q: %s", counted, got.stdout)
		}
	}
	got = w.run(context.Background(), "run", "get", "run-1", "--json")
	var snapshot pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(got.stdout), &snapshot); err != nil || !snapshot.GetTruncated() {
		t.Errorf("get --json hides truncation: %v %q", err, got.stdout)
	}
}

func TestGetOnAnUnknownRunListsTheCandidates(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, nil)}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), "run", "get", "nope")
	if got.code != 1 {
		t.Fatalf("exit %d", got.code)
	}
	contains(t, "stderr", got.stderr, `"nope"`, "run-1", "candidates")
}

// A run that stands still says so in the summary and carries the time in its JSON.
func TestGetSaysHowLongARunHasStoodStill(t *testing.T) {
	frame := snapshotFrame(5, false, []*pb.Run{stalledRun("run-1", 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))

	got := w.run(context.Background(), "run", "get", "run-1")
	contains(t, "summary", got.stdout, "running, stalled for 4m")

	got = w.run(context.Background(), "run", "get", "run-1", "--json")
	var snapshot pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(got.stdout), &snapshot); err != nil || snapshot.GetRuns()[0].GetStalledSince() == nil {
		t.Fatalf("stalled_since missing from %q: %v", got.stdout, err)
	}

	moving := snapshotFrame(5, false, []*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)})
	w = newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{moving}}))
	if got := w.run(context.Background(), "run", "get", "run-1"); strings.Contains(got.stdout, "stalled") {
		t.Errorf("a run that is moving is called stalled: %s", got.stdout)
	}
}

// The usage of a run and of each agent is what the orchestrator tallied, and it is shown even when a
// snapshot was cut. A provider that reports tokens and no dollars shows tokens, and never a zero cost.
func TestGetShowsTheUsageOfTheRunAndOfEachAgent(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	run.Usage = &pb.Usage{InputTokens: ptr(uint64(1_500_000)), OutputTokens: ptr(uint64(21_700)), CostUsd: ptr(0.3321)}
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)
	lead.Usage = &pb.Usage{CostUsd: ptr(0.3321), OutputTokens: ptr(uint64(900))}
	worker := agentOf("run-1", "worker-1", pb.AgentRole_AGENT_ROLE_WORKER, pb.AgentStatus_AGENT_STATUS_ENDED, 1)
	worker.Usage = &pb.Usage{InputTokens: ptr(uint64(630_000)), OutputTokens: ptr(uint64(4_100))}
	silent := agentOf("run-1", "judge-1", pb.AgentRole_AGENT_ROLE_JUDGE, pb.AgentStatus_AGENT_STATUS_IDLE, 2)
	for _, truncated := range []bool{false, true} {
		w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, truncated, []*pb.Run{run}, []*pb.Agent{lead, worker, silent})}}))
		got := w.run(context.Background(), "run", "get", "run-1")
		contains(t, "summary", got.stdout,
			"usage       cost $0.3321, 1.50M in, 21.7k out",
			"lead      lead-run-1  cost $0.3321, 900 out",
			"worker    worker-1  630.0k in, 4.1k out")
		if strings.Contains(got.stdout, "judge-1") || strings.Contains(got.stdout, "$0.0000") {
			t.Errorf("an agent with no usage is shown, or a cost is invented: %s", got.stdout)
		}
	}
	// A run that has reported nothing says so, and is not a zero.
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})}}))
	got := w.run(context.Background(), "run", "get", "run-1")
	contains(t, "summary", got.stdout, "usage       not reported yet")
}

// Against a whole daemon: what a finished turn cost is in the run's snapshot, so that a client that
// connects later reads the same totals, and so that the usage in `get --json` is the sum of the
// agents' usage.
func TestGetAndReplayShowTheUsageOfFinishedTurns(t *testing.T) {
	cost := 0.0301
	l := newLive(t, func(s *daemontest.Session, cmd *pb.SidecarCommand) {
		if st := cmd.GetStartTurn(); st != nil {
			s.Emit(&pb.SidecarEvent{Body: &pb.SidecarEvent_TurnEnded{TurnEnded: &pb.TurnEnded{
				TurnId: st.GetTurnId(), Status: pb.TurnStatus_TURN_STATUS_COMPLETED, Usage: &pb.Usage{CostUsd: &cost, OutputTokens: ptr(uint64(7))},
			}}})
			return
		}
		daemontest.DefaultReaction(s, cmd)
	})
	run, lead := l.startRun(t, "/work/repo", "alpha")
	w := newWorld(t, l.Listener.Dial)

	got := w.run(context.Background(), "run", "get", run)
	contains(t, "summary", got.stdout, "cost $0.0301", lead, "7 out")

	got = w.run(context.Background(), "run", "get", run, "--json")
	var snapshot pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(got.stdout), &snapshot); err != nil {
		t.Fatal(err)
	}
	if usage := snapshot.GetRuns()[0].GetUsage(); usage.GetCostUsd() != cost || usage.GetOutputTokens() != 7 {
		t.Errorf("run usage = %v, want cost %v and 7 output tokens", usage, cost)
	}
	var agents float64
	for _, a := range snapshot.GetAgents() {
		agents += a.GetUsage().GetCostUsd()
	}
	if agents != cost {
		t.Errorf("the agents' usage adds up to %v, the run says %v", agents, cost)
	}

	// A replay of the finished run shows what each turn cost, from the snapshot's turns.
	if stop := w.run(context.Background(), "run", "stop", run, "--yes"); stop.code != 0 {
		t.Fatalf("stop: exit %d, stderr %q", stop.code, stop.stderr)
	}
	l.awaitRun(t, run, hasStatus(pb.RunStatus_RUN_STATUS_ENDED))
	got = w.run(context.Background(), "run", "attach", run)
	contains(t, "replay", got.stdout, "turn 1 completed, cost $0.0301, 7 out")
}
