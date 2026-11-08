package daemon

import (
	"context"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// DescribeTask implements cliv1.DaemonServiceServer. The daemon is the only reader of task files: it
// answers the card, or why the task has none.
func (s *Server) DescribeTask(ctx context.Context, req *cliv1.DescribeTaskRequest) (*cliv1.DescribeTaskResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	resp, err := task.Describe(ctx, s.cardFiles, req)
	if err != nil {
		return nil, wire.Error(err)
	}
	if err := protovalidate.Validate(resp); err != nil {
		return nil, wire.Error(&core.Failure{Kind: core.FailureInternal, Message: "describe task: the answer is invalid: " + err.Error()})
	}
	return resp, nil
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
