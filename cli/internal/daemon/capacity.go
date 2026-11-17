package daemon

import (
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	"context"
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Capacity measures the capacity of a placement whose slots are a cluster's: its slots and what the
// cluster says, given the recent waits of the daemon's requests on the placement, newest first. It
// leaves the run's held allocations to the server, which knows them.
type Capacity func(ctx context.Context, waits []alloc.WaitSample) (*cliv1.PlacementCapacity, error)

// ReadPlacementCapacity implements cliv1.DaemonServiceServer.
func (s *Server) ReadPlacementCapacity(ctx context.Context, req *cliv1.ReadPlacementCapacityRequest) (*cliv1.ReadPlacementCapacityResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	placement := req.GetPlacement()
	var out *cliv1.PlacementCapacity
	if measure, ok := s.capacities[placement]; ok {
		c, err := measure(ctx, s.app.RecentWaits(placement))
		if err != nil {
			return nil, wire.Error(err)
		}
		out = c
	} else if total, free, ok := s.app.PlacementSlots(placement); ok {
		out = &cliv1.PlacementCapacity{Placement: placement, SlotsTotal: total, SlotsFree: free}
		for _, w := range s.app.RecentWaits(placement) {
			out.RecentWaits = append(out.RecentWaits, &cliv1.WaitSample{
				RequestedAt: timestamppb.New(w.RequestedAt), Waited: durationpb.New(w.Waited), RequestedFor: durationpb.New(w.RequestedFor),
			})
		}
	} else {
		return nil, wire.Error(&core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("placement %q is not in the operator config", placement)})
	}
	if pool, ok := s.app.PoolOf(placement); ok {
		out.GpuPool = gpuPoolOf(pool)
	}
	if run := req.GetRunId(); run != "" && s.app.HasAllocations(placement) {
		vs, err := s.app.Allocations(core.RunID(run), false)
		if err != nil {
			return nil, wire.Error(err)
		}
		for _, v := range vs {
			if v.Placement == placement {
				out.Held = append(out.Held, allocationOf(v))
			}
		}
	}
	return &cliv1.ReadPlacementCapacityResponse{Capacity: out}, nil
}

// gpuPoolOf is the contract's message for a placement's pool of GPUs.
func gpuPoolOf(p app.PoolView) *cliv1.GpuPoolCapacity {
	out := &cliv1.GpuPoolCapacity{Pool: string(p.Pool), Size: uint32(p.Size), Free: uint32(p.Free)}
	for _, h := range p.Holders {
		out.Holders = append(out.Holders, &cliv1.GpuHolder{JobId: string(h.Job), Kind: wire.JobKind(h.Kind), RunId: string(h.Run), Gpus: wire.GpuGrant(h.Grant)})
	}
	for _, w := range p.Waiting {
		out.Waiting = append(out.Waiting, &cliv1.GpuWaiter{JobId: string(w.Job), Wanted: uint32(w.Wanted)})
	}
	return out
}
