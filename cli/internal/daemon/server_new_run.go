package daemon

import (
	"context"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
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

// DescribeSetup implements cliv1.DaemonServiceServer. It reads, in one pass, what the setup checks
// of a draft task are derived from (git, the working tree, the store) and decides nothing; a draft
// whose files cannot be read yet answers why, as DescribeTask does.
func (s *Server) DescribeSetup(ctx context.Context, req *cliv1.DescribeSetupRequest) (*cliv1.DescribeSetupResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	if err := protovalidate.Validate(req); err != nil {
		return nil, wire.Error(&core.Failure{Kind: core.FailureInvalidInput, Message: "describe setup: " + err.Error()})
	}
	read, err := s.app.ReadSetup(ctx, app.Caller{Directory: req.GetRepository(), Task: req.GetTask()}, s.cardFiles)
	if err != nil {
		return nil, wire.Error(err)
	}
	resp := setupResponse(req.GetTask(), read)
	if err := protovalidate.Validate(resp); err != nil {
		return nil, wire.Error(&core.Failure{Kind: core.FailureInternal, Message: "describe setup: the answer is invalid: " + err.Error()})
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
