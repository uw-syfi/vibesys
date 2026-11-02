package daemon_test

import (
	"context"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
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
		{"same", daemon.ProtocolVersion, daemon.MinProtocolVersion, pb.Compatibility_COMPATIBILITY_COMPATIBLE},
		{"newer client that still speaks ours", daemon.ProtocolVersion + 3, daemon.ProtocolVersion, pb.Compatibility_COMPATIBILITY_COMPATIBLE},
		{"newer client that dropped ours", daemon.ProtocolVersion + 3, daemon.ProtocolVersion + 1, pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD},
	} {
		resp, err := rig.Service.Hello(context.Background(), hello(tc.client, tc.min))
		if err != nil || resp.GetCompatibility() != tc.want {
			t.Errorf("%s: got %v, %v; want %v", tc.name, resp, err, tc.want)
		}
		if resp.GetDaemon().GetProtocolVersion() != daemon.ProtocolVersion {
			t.Errorf("%s: the daemon reported protocol %d", tc.name, resp.GetDaemon().GetProtocolVersion())
		}
	}
	if _, err := rig.Service.Hello(context.Background(), &pb.HelloRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Errorf("a hello without versions gave %v, want InvalidArgument", err)
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

func TestPlansAreReadThroughTheReader(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, _ := beginRun(t, rig)
	rig.Plans.Set(run.ID, &pb.Plan{Number: 1, Title: "first"}, &pb.Plan{Number: 2, Title: "second"})
	list, err := rig.Service.ListPlans(context.Background(), &pb.ListPlansRequest{RunId: string(run.ID)})
	if err != nil || len(list.GetPlans()) != 2 {
		t.Fatalf("list: %v, %v", list, err)
	}
	view, err := rig.Service.ViewPlan(context.Background(), &pb.ViewPlanRequest{RunId: string(run.ID), Number: 2})
	if err != nil || view.GetPlan().GetTitle() != "second" {
		t.Fatalf("view: %v, %v", view, err)
	}
	if _, err := rig.Service.ViewPlan(context.Background(), &pb.ViewPlanRequest{RunId: string(run.ID), Number: 9}); status.Code(err) != codes.NotFound {
		t.Errorf("a missing plan gave %v, want NotFound", err)
	}
	if _, err := rig.Service.ListPlans(context.Background(), &pb.ListPlansRequest{RunId: "run-nobody"}); status.Code(err) != codes.NotFound {
		t.Errorf("a missing run gave %v, want NotFound", err)
	}
}

// Reading is the operator's alone: an agent that asks to read history or plans is refused.
func TestAnAgentCannotReadWhatOnlyTheOperatorMay(t *testing.T) {
	rig := daemontest.NewRig(t)
	run, lead := beginRun(t, rig)
	ctx := agentContext(lead.ID, lead.Session, rig.Auth.Token(core.Session{Agent: lead.ID, N: lead.Session}))
	if _, err := rig.AgentService.ListItems(ctx, &pb.ListItemsRequest{AgentId: string(lead.ID), BeforeOrdinal: 5, PageSize: 2}); status.Code(err) != codes.PermissionDenied {
		t.Errorf("ListItems by an agent gave %v, want PermissionDenied", err)
	}
	if _, err := rig.AgentService.ListPlans(ctx, &pb.ListPlansRequest{RunId: string(run.ID)}); status.Code(err) != codes.PermissionDenied {
		t.Errorf("ListPlans by an agent gave %v, want PermissionDenied", err)
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
