package client_test

import (
	"context"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/protocol"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

func isCheckIn(id string, state pb.CheckInState) func(*pb.RunChange) bool {
	return func(ev *pb.RunChange) bool {
		c := ev.GetCheckIn()
		return c != nil && c.GetId() == id && c.GetState() == state
	}
}

// A viewer catches up over the real service: the client sends who it is with every call, the receipt
// names the check-in at once, the facts arrive as a change to it, and a watcher that connects later is
// given the check-in whole. A client with no viewer is refused, and the error names the field.
func TestAViewerCatchesUpOverTheService(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*guard)
	defer cancel()
	rig := daemontest.NewRig(t)
	searches := new(search.Store)
	rig.Daemon.Search = searches
	live := rig.Serve(t)
	viewer := &client.Viewer{ID: "install-1", Label: "Ann's laptop"}
	op, err := client.Dial(ctx, live.Listener.Dial, client.Options{BinaryVersion: "test", Viewer: viewer})
	if err != nil {
		t.Fatal(err)
	}
	defer op.Close()
	anonymous, err := client.Dial(ctx, live.Listener.Dial, client.Options{BinaryVersion: "test"})
	if err != nil {
		t.Fatal(err)
	}
	defer anonymous.Close()

	// The watch is open before the run starts, so that no event of it is missed.
	feed := watchRuns(ctx, t, op, 0)
	<-feed.snap
	started, err := op.Dispatch(ctx, &pb.Command{Id: "start-1", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: "/repo", Task: "task", FanoutLimit: 1, Brief: "begin", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}})
	if err != nil || started.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("start: %v %v", started, err)
	}
	run := started.GetRunId()
	state, _ := rig.Daemon.State().Run(core.RunID(run))
	feed.until("the lead's first turn", turnDone(string(state.Lead), 1))
	searches.Put(core.RunID(run), search.Fold(nil))
	// The host's clock stands still unless a test moves it, and a window that has no time in it holds
	// nothing.
	rig.Clock.Advance(time.Minute)

	catchUp := &pb.Command{Id: "catch-up-1", Body: &pb.Command_RequestCatchUp{RequestCatchUp: &pb.RequestCatchUp{RunId: run}}}
	receipt, err := op.Dispatch(ctx, catchUp)
	if err != nil || receipt.GetCheckInId() == "" || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED || receipt.GetMessageId() == "" {
		t.Fatalf("catch up: %v %v", receipt, err)
	}
	made := feed.until("the check-in", isCheckIn(receipt.GetCheckInId(), pb.CheckInState_CHECK_IN_STATE_PENDING)).GetCheckIn()
	if made.GetViewer().GetId() != viewer.ID || made.GetViewer().GetLabel() != viewer.Label || made.GetAskMessageId() != receipt.GetMessageId() {
		t.Fatalf("the check-in %v does not say who pressed or what was asked", made)
	}
	done := feed.until("the facts", isCheckIn(receipt.GetCheckInId(), pb.CheckInState_CHECK_IN_STATE_READY)).GetCheckIn()
	if done.GetFacts().GetAgentsStarted() != 1 || done.GetSince().AsTime() != state.CreatedAt.UTC() {
		t.Fatalf("the facts of the first window: %v", done)
	}

	// A retry under the same id is the same receipt.
	if again, err := op.Dispatch(ctx, catchUp); err != nil || again.GetCheckInId() != receipt.GetCheckInId() {
		t.Fatalf("retry: %v %v", again, err)
	}

	// A client with no viewer is refused, with the field named.
	if _, err := anonymous.Dispatch(ctx, &pb.Command{Id: "catch-up-2", Body: &pb.Command_RequestCatchUp{RequestCatchUp: &pb.RequestCatchUp{RunId: run}}}); err == nil {
		t.Fatal("a catch-up with no viewer was carried out")
	} else if field, ok := client.BadField(err); !ok || field != protocol.MetaViewerID {
		t.Fatalf("the refusal names %q, %v: %v", field, ok, err)
	}

	// A watcher that connects later is given the check-in with its frozen facts.
	late := watchRuns(ctx, t, anonymous, 0)
	snapshot := <-late.snap
	if got := snapshot.GetCheckIns(); len(got) != 1 || got[0].GetId() != receipt.GetCheckInId() || got[0].GetFacts().GetAgentsStarted() != 1 {
		t.Fatalf("a late watcher's check-ins: %v", got)
	}
}
