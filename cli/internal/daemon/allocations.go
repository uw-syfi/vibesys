package daemon

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// allocationOf is an allocation as the contract states it.
func allocationOf(v app.AllocationView) *cliv1.Allocation {
	return wire.FromAllocationView(wire.AllocationView{Allocation: v.Allocation, Slurm: v.Slurm})
}

func allocationsOf(vs []app.AllocationView) []*cliv1.Allocation {
	out := make([]*cliv1.Allocation, 0, len(vs))
	for _, v := range vs {
		out = append(out, allocationOf(v))
	}
	return out
}

// RequestAllocation implements cliv1.DaemonServiceServer.
func (s *Server) RequestAllocation(ctx context.Context, req *cliv1.RequestAllocationRequest) (*cliv1.RequestAllocationResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	r := req.GetRequest()
	v, err := s.app.RequestAllocation(ctx, core.RunID(req.GetRunId()), r.GetPlacement(), r.GetRequestedFor().AsDuration())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.RequestAllocationResponse{Allocation: allocationOf(v)}, nil
}

// ListAllocations implements cliv1.DaemonServiceServer.
func (s *Server) ListAllocations(_ context.Context, req *cliv1.ListAllocationsRequest) (*cliv1.ListAllocationsResponse, error) {
	vs, err := s.app.Allocations(core.RunID(req.GetRunId()), req.GetEnded())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ListAllocationsResponse{Allocations: allocationsOf(vs)}, nil
}

// ShowAllocation implements cliv1.DaemonServiceServer.
func (s *Server) ShowAllocation(_ context.Context, req *cliv1.ShowAllocationRequest) (*cliv1.ShowAllocationResponse, error) {
	v, err := s.app.Allocation(alloc.ID(req.GetId()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ShowAllocationResponse{Allocation: allocationOf(v)}, nil
}

// WaitAllocation implements cliv1.DaemonServiceServer.
func (s *Server) WaitAllocation(ctx context.Context, req *cliv1.WaitAllocationRequest) (*cliv1.WaitAllocationResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	var until app.AllocUntil
	switch req.GetUntil().(type) {
	case *cliv1.WaitAllocationRequest_Running:
		until = app.AllocUntil{Kind: app.UntilRunning}
	case *cliv1.WaitAllocationRequest_EndingIn:
		until = app.AllocUntil{Kind: app.UntilEndingIn, EndingIn: req.GetEndingIn().AsDuration()}
	case *cliv1.WaitAllocationRequest_Ended:
		until = app.AllocUntil{Kind: app.UntilEnded}
	default:
		return nil, wire.Error(&core.Failure{Kind: core.FailureInvalidInput, Message: "until: say what to wait for: running, ending_in or ended"})
	}
	v, outcome, err := s.app.WaitAllocation(ctx, alloc.ID(req.GetId()), until, req.GetTimeout().AsDuration())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.WaitAllocationResponse{Allocation: allocationOf(v), Outcome: waitOutcomeOf(outcome)}, nil
}

func waitOutcomeOf(o app.AllocWaitOutcome) cliv1.AllocationWaitOutcome {
	switch o {
	case app.AllocWaitMet:
		return cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_MET
	case app.AllocWaitTimedOut:
		return cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_TIMED_OUT
	case app.AllocWaitEnded:
		return cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_ENDED
	}
	return cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_UNSPECIFIED
}

// EndAllocation implements cliv1.DaemonServiceServer.
func (s *Server) EndAllocation(ctx context.Context, req *cliv1.EndAllocationRequest) (*cliv1.EndAllocationResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	end := req.GetEnd()
	v, err := s.app.EndAllocation(ctx, core.RunID(req.GetRunId()), alloc.ID(end.GetId()), end.GetAfterJobs())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.EndAllocationResponse{Allocation: allocationOf(v)}, nil
}

// RenewAllocation implements cliv1.DaemonServiceServer.
func (s *Server) RenewAllocation(ctx context.Context, req *cliv1.RenewAllocationRequest) (*cliv1.RenewAllocationResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	var order *alloc.RenewalOrder
	if r := req.GetRenewal(); r != nil {
		o, err := wire.ToRenewalOrder(r)
		if err != nil {
			return nil, wire.Error(err)
		}
		order = &o
	}
	v, err := s.app.RenewAllocation(ctx, core.RunID(req.GetRunId()), alloc.ID(req.GetId()), order)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.RenewAllocationResponse{Allocation: allocationOf(v)}, nil
}

// EndRunAllocations implements cliv1.DaemonServiceServer.
func (s *Server) EndRunAllocations(ctx context.Context, req *cliv1.EndRunAllocationsRequest) (*cliv1.EndRunAllocationsResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	vs, err := s.app.EndRunAllocations(ctx, core.RunID(req.GetRunId()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.EndRunAllocationsResponse{Allocations: allocationsOf(vs)}, nil
}
