package clientcli_test

import (
	"context"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

func waitArgs(until string) []string {
	return []string{"run", "wait", "run-1", "--until", until, "--timeout", "90s"}
}

func TestWaitReturnsAtOnceWhenTheConditionAlreadyHolds(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	live := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	cases := map[string]*pb.WatchRunsResponse{
		"started": snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{live}),
		"report":  snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{live}, reportMessage("run-1", "m1", "done", 3)),
		"ended":   snapshotFrame(5, false, []*pb.Run{ended}, []*pb.Agent{live}),
	}
	for until, frame := range cases {
		t.Run(until, func(t *testing.T) {
			w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
			got := w.run(context.Background(), waitArgs(until)...)
			if got.code != 0 {
				t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
			}
			contains(t, "stdout", got.stdout, "run-1")
		})
	}
}

func TestWaitSubscribesAndReturnsWhenTheEventArrives(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	launching := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_LAUNCHING)
	launching.Capabilities = nil
	live := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_ACTIVE)
	f := &fakeService{frames: []*pb.WatchRunsResponse{
		snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{launching}),
		agentEvent(6, live, 1),
	}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), waitArgs("started")...)
	if got.code != 0 {
		t.Fatalf("exit %d, stderr %q", got.code, got.stderr)
	}
	contains(t, "stdout", got.stdout, "is live")
}

func TestWaitOnARunThatEndsFirstExitsOne(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	ended := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 0)
	ended.StopReason = pb.RunStopReason_RUN_STOP_REASON_OPERATOR
	f := &fakeService{frames: []*pb.WatchRunsResponse{
		snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}),
		runEvent(6, ended, 2),
	}}
	w := newWorld(t, serveFake(t, f))
	got := w.run(context.Background(), waitArgs("report")...)
	if got.code != 1 {
		t.Fatalf("exit %d, want 1; stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "ended before a report was posted")
}

func TestWaitForTheEndOfARunThatFailedExitsOne(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_FAILED, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_FAILED)
	lead.EndDetail = "no credentials"
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{lead})}}))
	got := w.run(context.Background(), waitArgs("ended")...)
	if got.code != 1 {
		t.Fatalf("exit %d, want 1", got.code)
	}
	contains(t, "stderr", got.stderr, "no credentials")
}

// The timeout is a bound on the wait, driven by the clock and not by the stream: nothing arrives, the
// clock fires, and the verb exits 3 whether or not it had read the snapshot yet.
func TestWaitTimesOutWithExitThree(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)})}}
	w := newWorld(t, serveFake(t, f))
	done := make(chan result, 1)
	go func() { done <- w.run(context.Background(), waitArgs("report")...) }()
	w.clock.fire(t)
	got := <-done
	if got.code != 3 {
		t.Fatalf("exit %d, want 3; stderr %q", got.code, got.stderr)
	}
	contains(t, "stderr", got.stderr, "timed out after 1m30s", "a report was posted")
}

func TestWaitJSONIsTheSnapshotOfTheRun(t *testing.T) {
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)
	frame := snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)}, reportMessage("run-1", "m1", "done", 3))
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{frame}}))
	got := w.run(context.Background(), append(waitArgs("report"), "--json")...)
	var snapshot pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(got.stdout), &snapshot); err != nil || len(snapshot.GetMessages()) != 1 {
		t.Fatalf("stdout %q: %v", got.stdout, err)
	}
}

func TestWaitNeedsAGoalAndABound(t *testing.T) {
	w := newWorld(t, serveFake(t, &fakeService{}))
	for _, args := range [][]string{
		{"run", "wait", "--timeout", "1m"},
		{"run", "wait", "--until", "report"},
		{"run", "wait", "--until", "later", "--timeout", "1m"},
		{"run", "wait", "--until", "report", "--timeout", "0s"},
	} {
		if got := w.run(context.Background(), args...); got.code != 2 {
			t.Errorf("%v: exit %d, want 2", args, got.code)
		}
	}
}
