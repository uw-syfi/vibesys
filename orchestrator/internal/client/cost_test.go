package client_test

import (
	"context"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// A client reads a run's cost over the real service: the whole view first, a whole view again when a
// turn ends, a fresh one when the host's clock has moved on while the run lives, and a failure for a
// run the host does not have. The frames cross a real gRPC connection, so the method is registered,
// the request is validated and the stream wakes on events and on the loop's own ticks.
func TestAClientReadsARunsCostOverTheService(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*guard)
	defer cancel()
	rig := daemontest.NewRig(t)
	price, limit := 0.5, 100.0
	rig.Sidecars.React = func(s *daemontest.Session, cmd *pb.SessionCommand) {
		if turn := cmd.GetStartTurn(); turn != nil {
			daemontest.AnswerTurnWith(s, turn.GetTurnId(), "ok", &pb.Usage{CostUsd: &price})
			return
		}
		daemontest.DefaultReaction(s, cmd)
	}
	live := rig.Serve(t)
	op, err := client.Dial(ctx, live.Listener.Dial, client.Options{BinaryVersion: "test"})
	if err != nil {
		t.Fatal(err)
	}
	defer op.Close()

	// The watch is open before the run starts: a watch opened after would miss the lead's first turn if the
	// host finished it first, and the test would wait for an event that was already past.
	feed := watchRuns(ctx, t, op, 0)
	<-feed.snap
	receipt, err := op.Dispatch(ctx, &pb.Command{Id: "start-1", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: "/repo", Task: "task", FanoutLimit: 1, Brief: "begin",
		Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE}, MaxCostUsd: &limit,
	}}})
	if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("start: %v %v", receipt, err)
	}
	run := receipt.GetRunId()
	state, _ := rig.Daemon.State().Run(core.RunID(run))
	feed.until("the lead's first turn", turnDone(string(state.Lead), 1))

	stream, err := op.WatchRunCost(ctx, run)
	if err != nil {
		t.Fatal(err)
	}
	next := func(what string) *pb.RunCost {
		t.Helper()
		frame, err := stream.Recv()
		if err != nil {
			t.Fatalf("waiting for %s: %v", what, err)
		}
		return frame.GetCost()
	}
	first := next("the first view")
	if first.GetRunId() != run || first.GetTotal().GetCostUsd() != price || first.GetMaxCostUsd() != limit || first.GetTurns() != 1 {
		t.Fatalf("first view %v, want the run's one priced turn and its limit", first)
	}

	// A turn that ends sends a whole view with it in.
	if _, err := op.Dispatch(ctx, &pb.Command{Id: "tell-1", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(state.Lead), Text: "again"}}}); err != nil {
		t.Fatal(err)
	}
	var second *pb.RunCost
	for second = first; second.GetTurns() < 2; {
		second = next("the view with the second turn")
	}
	if second.GetTotal().GetCostUsd() != 2*price || second.GetSequence() <= first.GetSequence() {
		t.Fatalf("second view %v, want two turns at %v and a later sequence than %d", second, price, first.GetSequence())
	}

	// With nothing happening, the host's clock moving on is a reason for a new view of a live run.
	rig.Clock.Advance(daemon.CostRefresh)
	live.Tick()
	refreshed := second
	for !refreshed.GetAsOf().AsTime().After(second.GetAsOf().AsTime()) {
		refreshed = next("the refresh")
	}
	if refreshed.GetTotal().GetCostUsd() != 2*price || refreshed.GetTurns() != second.GetTurns() {
		t.Fatalf("refresh %v is not the same run as %v, only later", refreshed, second)
	}

	// A stream's failure arrives with its first receive.
	missing, err := op.WatchRunCost(ctx, "no-such-run")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := missing.Recv(); status.Code(err) != codes.NotFound {
		t.Fatalf("the cost of a run the host does not have: %v", err)
	}
}
