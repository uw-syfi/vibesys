package daemon_test

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/protocol"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

func hello(client, min uint32) *pb.HelloRequest {
	return &pb.HelloRequest{Client: &pb.BuildVersions{BinaryVersion: "test", ProtocolVersion: client, MinProtocolVersion: min}}
}

// A client and the daemon agree when each is within the other's range, and the answer says which
// side to upgrade when they are not.
func TestHelloSaysWhichSideIsTooOld(t *testing.T) {
	rig := daemontest.NewRig(t)
	for _, tc := range []struct {
		name        string
		client, min uint32
		want        pb.Compatibility
	}{
		{"same", protocol.Version, protocol.MinVersion, pb.Compatibility_COMPATIBILITY_COMPATIBLE},
		{"newer client that still speaks ours", protocol.Version + 3, protocol.Version, pb.Compatibility_COMPATIBILITY_COMPATIBLE},
		{"newer client that dropped ours", protocol.Version + 3, protocol.Version + 1, pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD},
	} {
		resp, err := rig.Service.Hello(context.Background(), hello(tc.client, tc.min))
		if err != nil || resp.GetCompatibility() != tc.want {
			t.Errorf("%s: got %v, %v; want %v", tc.name, resp, err, tc.want)
		}
		if resp.GetDaemon().GetProtocolVersion() != protocol.Version {
			t.Errorf("%s: the daemon reported protocol %d", tc.name, resp.GetDaemon().GetProtocolVersion())
		}
	}
	if _, err := rig.Service.Hello(context.Background(), &pb.HelloRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Errorf("a hello without versions gave %v, want InvalidArgument", err)
	}
}

// A fan-out of zero can never admit a worker: the first spawn would already be at the limit. Both
// ways to enter optimization refuse that malformed command instead of committing a run that cannot
// do work.
func TestEnteringOptimizationRefusesZeroFanout(t *testing.T) {
	rig := daemontest.NewRig(t)
	for _, command := range []*pb.Command{
		{Id: "start", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
			Repository: "/repo", Task: "task", FanoutLimit: 0, Brief: "go",
			Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
		}}},
		{Id: "complete", Body: &pb.Command_CompleteSetup{CompleteSetup: &pb.CompleteSetup{
			RunId: "setup", Task: "task", FanoutLimit: 0, Brief: "go",
			Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
		}}},
	} {
		_, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: command})
		if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), "fanout_limit") {
			t.Errorf("%T with zero fan-out gave %v, want InvalidArgument naming fanout_limit", command.GetBody(), err)
		}
	}
	if runs := rig.Daemon.State().Runs(); len(runs) != 0 {
		t.Fatalf("the refused commands committed %d runs, want none", len(runs))
	}
}

func beginRun(t *testing.T, rig *daemontest.Rig) (core.Run, core.Agent) {
	t.Helper()
	resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: "start", Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: "/repo", Task: "task", FanoutLimit: 1, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}}})
	if err != nil {
		t.Fatal(err)
	}
	rig.Settle(t)
	run, _ := rig.Daemon.State().Run(core.RunID(resp.GetReceipt().GetRunId()))
	lead, _ := rig.Daemon.State().Agent(run.Lead)
	return run, lead
}

// History comes a page at a time, newest first, each page naming where the next begins, and a
// reader that follows the pages sees every item once.
func TestListItemsPagesBackThroughHistory(t *testing.T) {
	rig := daemontest.NewRig(t)
	_, lead := beginRun(t, rig)
	for i := range 6 {
		if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
			Id: "tell-" + string(rune('a'+i)), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "again"}}}}); err != nil {
			t.Fatal(err)
		}
		rig.Settle(t)
	}
	// The newest page comes with the watch's snapshot; ListItems goes back from its cursor.
	w, first, err := rig.Service.WatchAgentFrom(lead.ID, 2, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	var seen []uint32
	newest := first[0].GetPage()
	for _, it := range newest.GetItems() {
		seen = append(seen, it.GetOrdinal())
	}
	before := newest.GetNextBeforeOrdinal()
	for pages := 0; before != 0; pages++ {
		if pages > 50 {
			t.Fatal("paging never reached the oldest item")
		}
		resp, err := rig.Service.ListItems(context.Background(), &pb.ListItemsRequest{AgentId: string(lead.ID), BeforeOrdinal: before, PageSize: 2})
		if err != nil {
			t.Fatal(err)
		}
		page := resp.GetPage()
		if len(page.GetItems()) > 2 {
			t.Fatalf("page of %d items for a page size of 2", len(page.GetItems()))
		}
		for _, it := range page.GetItems() {
			seen = append(seen, it.GetOrdinal())
		}
		before = page.GetNextBeforeOrdinal()
	}
	if len(seen) < 6 {
		t.Fatalf("paging read %d items, want at least 6", len(seen))
	}
	have := map[uint32]bool{}
	for _, o := range seen {
		if have[o] {
			t.Fatalf("ordinal %d appeared on two pages: %v", o, seen)
		}
		have[o] = true
	}
	if _, err := rig.Service.ListItems(context.Background(), &pb.ListItemsRequest{AgentId: "agent-nobody", BeforeOrdinal: 5, PageSize: 2}); status.Code(err) != codes.NotFound {
		t.Errorf("an unknown agent gave %v, want NotFound", err)
	}
}

// Reading is the operator's alone: an agent that asks to read history is refused.
func TestAnAgentCannotReadWhatOnlyTheOperatorMay(t *testing.T) {
	rig := daemontest.NewRig(t)
	_, lead := beginRun(t, rig)
	ctx := agentContext(lead.ID, lead.Session, rig.Auth.Token(core.Session{Agent: lead.ID, N: lead.Session}))
	if _, err := rig.AgentService.ListItems(ctx, &pb.ListItemsRequest{AgentId: string(lead.ID), BeforeOrdinal: 5, PageSize: 2}); status.Code(err) != codes.PermissionDenied {
		t.Errorf("ListItems by an agent gave %v, want PermissionDenied", err)
	}
}

// A client that claims to have read past the end of the log, as after the daemon lost its store,
// is told to start again from a snapshot, not left waiting for events that will never come.
func TestAClientAheadOfTheLogIsToldToStartOver(t *testing.T) {
	rig := daemontest.NewRig(t)
	beginRun(t, rig)
	if _, _, err := rig.Service.WatchRunsFrom(core.Operator{}, rig.Daemon.Last()+10); err == nil {
		t.Fatal("a watch ahead of the log was accepted")
	}
}

// A client that connects late learns what the run has already said and done from the snapshot: the
// contract puts every message and turn of a run in it, however many pages the messages fill. A
// snapshot without them left a client that opened after the lead reported with no report to show.
func TestTheRunsSnapshotHoldsEveryMessageAndTurnOfTheRun(t *testing.T) {
	for _, extra := range []int{0, 1, 5, store.MaxPageMessages + 3} {
		t.Run(fmt.Sprintf("%d messages sent after the start", extra), func(t *testing.T) {
			rig := daemontest.NewRig(t)
			run, lead := beginRun(t, rig)
			for i := range extra {
				if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
					Id: fmt.Sprintf("tell-%d", i), Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "again"}}}}); err != nil {
					t.Fatal(err)
				}
				rig.Settle(t)
			}
			wantMessages, wantTurns := storedHistory(t, rig, run.ID)
			if len(wantMessages) < extra+1 || len(wantTurns) == 0 {
				t.Fatalf("the run holds %d messages and %d turns; the test needs the history it checks", len(wantMessages), len(wantTurns))
			}
			w, first, err := rig.Service.WatchRunsFrom(core.Operator{}, 0)
			if err != nil {
				t.Fatal(err)
			}
			defer w.Close()
			snap := client.WatchRunsSnapshot(first[0])
			assertSame(t, "messages", snap.GetMessages(), wantMessages)
			assertSame(t, "turns", snap.GetTurns(), wantTurns)
		})
	}
}

// storedHistory is what the store holds of a run's messages and turns, as the contract states them.
func storedHistory(t *testing.T, rig *daemontest.Rig, run core.RunID) ([]*pb.Message, []*pb.Turn) {
	t.Helper()
	var messages []*pb.Message
	for after := uint64(0); ; {
		page, err := rig.Store.Messages(run, after, store.MaxPageMessages)
		if err != nil {
			t.Fatal(err)
		}
		for _, rec := range page {
			messages = append(messages, wire.MailboxMessage(rec.Message))
			after = rec.Seq
		}
		if len(page) < store.MaxPageMessages {
			break
		}
	}
	window, err := rig.Store.Turns(run, time.Time{}, time.Date(9999, time.December, 31, 0, 0, 0, 0, time.UTC), store.MaxWindowTurns)
	if err != nil {
		t.Fatal(err)
	}
	var turns []*pb.Turn
	for _, turn := range window.Turns {
		turns = append(turns, wire.TurnMessage(turn))
	}
	return messages, turns
}

func assertSame[M proto.Message](t *testing.T, what string, got, want []M) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("the snapshot holds %d %s, the store holds %d", len(got), what, len(want))
	}
	for i := range want {
		if !proto.Equal(got[i], want[i]) {
			t.Fatalf("%s[%d]: the snapshot holds %v, the store holds %v", what, i, got[i], want[i])
		}
	}
}
