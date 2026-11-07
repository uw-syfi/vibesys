package daemon

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// StartWorkstream implements cliv1.DaemonServiceServer.
func (s *Server) StartWorkstream(ctx context.Context, req *cliv1.StartWorkstreamRequest) (*cliv1.StartWorkstreamResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	started, err := s.app.StartWorkstream(ctx, call, req.GetTitle())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.StartWorkstreamResponse{Workstream: wire.Workstream(started.Workstream), Created: started.Created}, nil
}

// ListWorkstreams implements cliv1.DaemonServiceServer.
func (s *Server) ListWorkstreams(ctx context.Context, req *cliv1.ListWorkstreamsRequest) (*cliv1.ListWorkstreamsResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	filter, err := wire.ToWorkstreamFilter(req)
	if err != nil {
		return nil, wire.Error(err)
	}
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	workstreams, err := s.app.Workstreams(ctx, call.Scope, filter)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ListWorkstreamsResponse{Workstreams: wire.Workstreams(workstreams)}, nil
}

// EditWorkstream implements cliv1.DaemonServiceServer.
func (s *Server) EditWorkstream(ctx context.Context, req *cliv1.EditWorkstreamRequest) (*cliv1.EditWorkstreamResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	change := app.WorkstreamChange{Title: req.Title, Note: req.Note}
	if req.Status != nil {
		status, err := wire.ToWorkstreamStatus("status", req.GetStatus())
		if err != nil {
			return nil, wire.Error(err)
		}
		change.Status = &status
	}
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	w, err := s.app.EditWorkstream(ctx, call, core.WorkstreamNumber(req.GetNumber()), change)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.EditWorkstreamResponse{Workstream: wire.Workstream(w)}, nil
}
