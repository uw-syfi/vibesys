package daemon

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Show implements cliv1.DaemonServiceServer.
func (s *Server) Show(ctx context.Context, req *cliv1.ShowRequest) (*cliv1.ShowResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	results, err := s.app.Show(ctx, scopeOf(req.GetContext()), core.Commit(req.GetCommit()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ShowResponse{Results: wire.Results(results)}, nil
}

// Compare implements cliv1.DaemonServiceServer.
func (s *Server) Compare(ctx context.Context, req *cliv1.CompareRequest) (*cliv1.CompareResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	comparison, err := s.app.Compare(ctx, wire.ToCallContext(req.GetContext()), core.Commit(req.GetCommit()), core.Commit(req.GetAgainst()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.CompareResponse{Comparison: wire.Comparison(comparison)}, nil
}

// Promote implements cliv1.DaemonServiceServer. A refusal is the response; an error is a failure.
func (s *Server) Promote(ctx context.Context, req *cliv1.PromoteRequest) (*cliv1.PromoteResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	decision, err := s.app.Promote(ctx, wire.ToCallContext(req.GetContext()), core.Commit(req.GetCommit()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return wire.PromoteResponse(decision), nil
}

// CurrentResult implements cliv1.DaemonServiceServer.
func (s *Server) CurrentResult(ctx context.Context, req *cliv1.CurrentResultRequest) (*cliv1.CurrentResultResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	answer, err := s.app.CurrentResult(ctx, wire.ToCallContext(req.GetContext()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return wire.CurrentResultResponse(answer), nil
}
