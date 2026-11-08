package daemon

import (
	"context"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DescribeTask implements cliv1.DaemonServiceServer. It answers Unimplemented until the task reader
// builds cards.
func (s *Server) DescribeTask(context.Context, *cliv1.DescribeTaskRequest) (*cliv1.DescribeTaskResponse, error) {
	return nil, status.Error(codes.Unimplemented, "DescribeTask is not implemented by this daemon yet")
}

// ListPlacements implements cliv1.DaemonServiceServer. It answers Unimplemented until the placements
// report the hardware they give.
func (s *Server) ListPlacements(context.Context, *cliv1.ListPlacementsRequest) (*cliv1.ListPlacementsResponse, error) {
	return nil, status.Error(codes.Unimplemented, "ListPlacements is not implemented by this daemon yet")
}
