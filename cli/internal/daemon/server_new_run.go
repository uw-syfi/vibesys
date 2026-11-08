package daemon

import (
	"context"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// DescribeTask implements cliv1.DaemonServiceServer. It answers Unimplemented until the task reader
// builds cards.
func (s *Server) DescribeTask(context.Context, *cliv1.DescribeTaskRequest) (*cliv1.DescribeTaskResponse, error) {
	return nil, status.Error(codes.Unimplemented, "DescribeTask is not implemented by this daemon yet")
}

// DescribeSetup implements cliv1.DaemonServiceServer. It answers Unimplemented until the daemon reads
// the setup facts.
func (s *Server) DescribeSetup(context.Context, *cliv1.DescribeSetupRequest) (*cliv1.DescribeSetupResponse, error) {
	return nil, status.Error(codes.Unimplemented, "DescribeSetup is not implemented by this daemon yet")
}

// ListPlacements implements cliv1.DaemonServiceServer.
func (s *Server) ListPlacements(ctx context.Context, req *cliv1.ListPlacementsRequest) (*cliv1.ListPlacementsResponse, error) {
	if s.placements == nil {
		return nil, status.Error(codes.Unavailable, "placement reader is not configured")
	}
	placements, err := s.placements.List(ctx, req.GetRefresh())
	if err != nil {
		return nil, status.FromContextError(err).Err()
	}
	out := make([]*cliv1.PlacementView, len(placements))
	for i, r := range placements {
		out[i] = wire.PlacementView(r)
	}
	return &cliv1.ListPlacementsResponse{Placements: out}, nil
}
