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
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	results, err := s.app.Show(ctx, call.Scope, core.Commit(req.GetCommit()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ShowResponse{Results: wire.Results(results), Measurements: wire.Measurements(core.PoolMeasurements(results))}, nil
}

// Compare implements cliv1.DaemonServiceServer.
func (s *Server) Compare(ctx context.Context, req *cliv1.CompareRequest) (*cliv1.CompareResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	comparison, err := s.app.Compare(ctx, call, core.Commit(req.GetCommit()), core.Commit(req.GetAgainst()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.CompareResponse{Comparison: wire.Comparison(comparison)}, nil
}

// Promote implements cliv1.DaemonServiceServer. A refusal is the response; an error is a failure.
func (s *Server) Promote(ctx context.Context, req *cliv1.PromoteRequest) (*cliv1.PromoteResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	decision, frontier, err := s.app.Promote(ctx, call, core.Commit(req.GetCommit()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return wire.PromoteResponse(decision, frontier), nil
}

// AddToFrontier implements cliv1.DaemonServiceServer. A refusal is the response; an error is a
// failure.
func (s *Server) AddToFrontier(ctx context.Context, req *cliv1.AddToFrontierRequest) (*cliv1.AddToFrontierResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	decision, err := s.app.AddToFrontier(ctx, call, core.Commit(req.GetCommit()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.AddToFrontierResponse{Decision: wire.FrontierDecision(decision)}, nil
}

// ListFrontier implements cliv1.DaemonServiceServer.
func (s *Server) ListFrontier(ctx context.Context, req *cliv1.ListFrontierRequest) (*cliv1.ListFrontierResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	entries, err := s.app.Frontier(ctx, call)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ListFrontierResponse{Members: wire.FrontierEntries(entries)}, nil
}

// CurrentResult implements cliv1.DaemonServiceServer.
func (s *Server) CurrentResult(ctx context.Context, req *cliv1.CurrentResultRequest) (*cliv1.CurrentResultResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	answer, err := s.app.CurrentResult(ctx, call)
	if err != nil {
		return nil, wire.Error(err)
	}
	return wire.CurrentResultResponse(answer), nil
}
