package clientcli_test

import (
	"context"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
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
