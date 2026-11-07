package daemon

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// DeclareBound implements cliv1.DaemonServiceServer.
func (s *Server) DeclareBound(ctx context.Context, req *cliv1.DeclareBoundRequest) (*cliv1.DeclareBoundResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	bound, err := s.app.DeclareBound(ctx, call, core.Commit(req.GetCommit()), core.BoundStatement{
		Objective: req.GetObjective(), Value: req.GetValue(), Unit: req.GetUnit(), Note: req.GetNote(),
	})
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.DeclareBoundResponse{Bound: wire.Bound(bound)}, nil
}

// ListBounds implements cliv1.DaemonServiceServer.
func (s *Server) ListBounds(ctx context.Context, req *cliv1.ListBoundsRequest) (*cliv1.ListBoundsResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	bounds, err := s.app.Bounds(ctx, call.Scope)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ListBoundsResponse{Bounds: wire.Bounds(bounds)}, nil
}
