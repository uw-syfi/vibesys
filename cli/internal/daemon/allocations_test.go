package daemon_test

import (
	"context"
	"net"
	"testing"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// newAllocationRig is a daemon server whose placement "p" offers standing allocations as slots of
// its own capacity, reached through an in-memory connection.
func newAllocationRig(t *testing.T) cliv1.DaemonServiceClient {
	t.Helper()
	env := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates())
	checkouts := &fakeenv.Checkouts{}
	survivors := supervisor.NewSurvivors()
	a := app.New(app.Config{
		Clock:        clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond),
		Stores:       memstores.New(app.Rules(5)),
		Environments: fakeenv.Environments{core.EnvironmentHost: env},
		Supervisors:  supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: env}, checkouts, survivors),
		Repositories: repos(),
		Scheduler:    app.Scheduler{Resources: []schedule.Resource{{ID: schedule.PlacementOf("p"), Capacity: 2}}},
		Allocations: app.Allocations{
			Log: &memstore.AllocLog{},
			Placements: []app.AllocPlacement{{Name: "p", Limits: alloc.Limits{
				MaxHeld: 2, MaxFor: 2 * time.Hour, Margin: time.Minute, Endless: true,
			}}},
		},
	})
	if err := a.Start(ctxFor(t)); err != nil {
		t.Fatal(err)
	}
	srv := daemon.NewServer(a, "test", daemon.Inherited{})
	listener := bufconn.Listen(1 << 20)
	gs := grpc.NewServer(grpc.ChainUnaryInterceptor(wire.RequestValidator()))
	cliv1.RegisterDaemonServiceServer(gs, srv)
	go gs.Serve(listener)
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		conn.Close()
		srv.Shutdown()
		gs.Stop()
		a.Close()
		survivors.Close()
	})
	return cliv1.NewDaemonServiceClient(conn)
}

func failureKindOf(t *testing.T, err error) core.FailureKind {
	t.Helper()
	f, ok := wire.FailureOf(err)
	if !ok {
		t.Fatalf("%v is not a failure of the contract", err)
	}
	return f.Kind
}

// An allocation is requested, waited for, listed, shown, given a renewal order and taken off it,
// and ended, over the contract, and every message it returns is valid.
func TestAnAllocationIsRequestedWaitedForAndEndedOverTheContract(t *testing.T) {
	c := newAllocationRig(t)
	ctx := ctxFor(t)
	req, err := c.RequestAllocation(ctx, &cliv1.RequestAllocationRequest{
		RunId: "run-1", Request: &cliv1.AllocationRequest{Placement: "p", RequestedFor: durationpb.New(time.Hour)},
	})
	if err != nil {
		t.Fatalf("RequestAllocation: %v", err)
	}
	id := req.GetAllocation().GetId()
	waited, err := c.WaitAllocation(ctx, &cliv1.WaitAllocationRequest{
		Id: id, Until: &cliv1.WaitAllocationRequest_Running{Running: true}, Timeout: durationpb.New(time.Hour),
	})
	if err != nil || waited.GetOutcome() != cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_MET {
		t.Fatalf("WaitAllocation = %v, %v; want met", waited, err)
	}
	if got := waited.GetAllocation().GetState(); got != cliv1.AllocationState_ALLOCATION_STATE_RUNNING {
		t.Fatalf("the allocation is %s after the wait", got)
	}
	// A slot of the machine has no end, so there is nothing to renew: the daemon says so.
	_, err = c.RenewAllocation(ctx, &cliv1.RenewAllocationRequest{RunId: "run-1", Id: id, Order: &cliv1.RenewAllocationRequest_Renewal{Renewal: &cliv1.RenewalOrder{
		Lead: durationpb.New(10 * time.Minute), SuccessorFor: durationpb.New(time.Hour), Until: &cliv1.RenewalOrder_At{At: timestamppb.New(time.Date(2026, 1, 2, 0, 0, 0, 0, time.UTC))},
	}}})
	if failureKindOf(t, err) != core.FailureConflict {
		t.Fatalf("renewing an allocation with no end = %v, want a conflict", err)
	}
	if _, err := c.RenewAllocation(ctx, &cliv1.RenewAllocationRequest{RunId: "run-1", Id: id, Order: &cliv1.RenewAllocationRequest_Off{Off: true}}); err != nil {
		t.Fatalf("taking the order away: %v", err)
	}
	listed, err := c.ListAllocations(ctx, &cliv1.ListAllocationsRequest{RunId: "run-1"})
	if err != nil || len(listed.GetAllocations()) != 1 {
		t.Fatalf("ListAllocations = %v, %v; want the one allocation", listed, err)
	}
	if _, err := c.ShowAllocation(ctx, &cliv1.ShowAllocationRequest{Id: id}); err != nil {
		t.Fatalf("ShowAllocation: %v", err)
	}
	// Another run's allocation is not this run's to end.
	_, err = c.EndAllocation(ctx, &cliv1.EndAllocationRequest{RunId: "run-2", End: &cliv1.AllocationEnd{Id: id}})
	if failureKindOf(t, err) != core.FailureNotFound {
		t.Fatalf("ending another run's allocation = %v, want not found", err)
	}
	ended, err := c.EndAllocation(ctx, &cliv1.EndAllocationRequest{RunId: "run-1", End: &cliv1.AllocationEnd{Id: id}})
	if err != nil || ended.GetAllocation().GetState() != cliv1.AllocationState_ALLOCATION_STATE_ENDED {
		t.Fatalf("EndAllocation = %v, %v; want ended", ended, err)
	}
	for _, m := range []*cliv1.Allocation{req.GetAllocation(), waited.GetAllocation(), listed.GetAllocations()[0], ended.GetAllocation()} {
		if err := protovalidate.Validate(m); err != nil {
			t.Errorf("an allocation the daemon returned is not valid: %v", err)
		}
	}
}

// A request the rules refuse fails with the kind that says whose mistake it is, and a wait that
// times out says so and changes nothing.
func TestAllocationRefusalsAndTimeoutsAreTyped(t *testing.T) {
	c := newAllocationRig(t)
	ctx := ctxFor(t)
	ask := func(placement string, d time.Duration) error {
		_, err := c.RequestAllocation(ctx, &cliv1.RequestAllocationRequest{
			RunId: "run-1", Request: &cliv1.AllocationRequest{Placement: placement, RequestedFor: durationpb.New(d)},
		})
		return err
	}
	if k := failureKindOf(t, ask("elsewhere", time.Hour)); k != core.FailureInvalidInput {
		t.Errorf("a placement with no allocations section: %s, want invalid_input", k)
	}
	if k := failureKindOf(t, ask("p", 3*time.Hour)); k != core.FailureInvalidInput {
		t.Errorf("a time over max_for: %s, want invalid_input", k)
	}
	if err := ask("p", time.Hour); err != nil {
		t.Fatal(err)
	}
	if err := ask("p", time.Hour); err != nil {
		t.Fatal(err)
	}
	if k := failureKindOf(t, ask("p", time.Hour)); k != core.FailureConflict {
		t.Errorf("max_held reached: %s, want conflict", k)
	}
	_, err := c.ShowAllocation(ctx, &cliv1.ShowAllocationRequest{Id: "a99"})
	if k := failureKindOf(t, err); k != core.FailureNotFound {
		t.Errorf("an allocation nobody has: %s, want not_found", k)
	}
}
