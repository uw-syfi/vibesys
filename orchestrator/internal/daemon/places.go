package daemon

import (
	"context"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// PlaceSource lists the places evaluations can run and the hardware each gives. Refresh probes now;
// otherwise it serves what the last probe found. An unreachable or unreadable place is a place with an
// unreadable state, not an error: an error is a failure to ask at all.
type PlaceSource interface {
	Places(ctx context.Context, refresh bool) ([]*pb.EvalPlace, error)
}

// ListEvalPlaces implements OrchestratorService: the places a run's candidates can be evaluated on, for
// the operator. It answers Unimplemented on a host with no place source, and Unavailable when the
// source cannot be asked (the CLI daemon is not there).
func (s *Service) ListEvalPlaces(ctx context.Context, req *pb.ListEvalPlacesRequest) (*pb.ListEvalPlacesResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "ListEvalPlaces"); err != nil {
		return nil, err
	}
	if s.Places == nil {
		return nil, unimplemented("ListEvalPlaces")
	}
	places, err := bound(s.Bounds, ctx, func(c context.Context) ([]*pb.EvalPlace, error) {
		return s.Places.Places(c, req.GetRefresh())
	})
	if err != nil {
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	resp := &pb.ListEvalPlacesResponse{Places: places}
	if err := wire.Validate(resp); err != nil {
		return nil, status.Errorf(codes.Internal, "the host's places are not valid: %v", err)
	}
	return resp, nil
}
