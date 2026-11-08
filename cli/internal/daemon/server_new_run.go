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

// ListPlacements implements cliv1.DaemonServiceServer. It answers Unimplemented until the placements
// report the hardware they give.
func (s *Server) ListPlacements(context.Context, *cliv1.ListPlacementsRequest) (*cliv1.ListPlacementsResponse, error) {
	return nil, status.Error(codes.Unimplemented, "ListPlacements is not implemented by this daemon yet")
}
