package daemon

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// CreatePlan implements cliv1.DaemonServiceServer.
func (s *Server) CreatePlan(ctx context.Context, req *cliv1.CreatePlanRequest) (*cliv1.CreatePlanResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	plan, err := s.app.CreatePlan(ctx, wire.ToCallContext(req.GetContext()), req.GetTitle(), req.GetBody())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.CreatePlanResponse{Plan: wire.Plan(plan)}, nil
}

// ListPlans implements cliv1.DaemonServiceServer.
func (s *Server) ListPlans(ctx context.Context, req *cliv1.ListPlansRequest) (*cliv1.ListPlansResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	filter, err := wire.ToPlanFilter(req)
	if err != nil {
		return nil, wire.Error(err)
	}
	plans, err := s.app.Plans(ctx, scopeOf(req.GetContext()), filter)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ListPlansResponse{Plans: wire.Plans(plans)}, nil
}

// ViewPlan implements cliv1.DaemonServiceServer.
func (s *Server) ViewPlan(ctx context.Context, req *cliv1.ViewPlanRequest) (*cliv1.ViewPlanResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	view, err := s.app.ViewPlan(ctx, scopeOf(req.GetContext()), core.PlanNumber(req.GetNumber()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ViewPlanResponse{View: wire.PlanView(view)}, nil
}

// EditPlan implements cliv1.DaemonServiceServer.
func (s *Server) EditPlan(ctx context.Context, req *cliv1.EditPlanRequest) (*cliv1.EditPlanResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	change := app.PlanChange{Title: req.Title, Body: req.Body}
	if req.Status != nil {
		status, err := wire.ToPlanStatus("status", req.GetStatus())
		if err != nil {
			return nil, wire.Error(err)
		}
		change.Status = &status
	}
	plan, err := s.app.EditPlan(ctx, wire.ToCallContext(req.GetContext()), core.PlanNumber(req.GetNumber()), change)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.EditPlanResponse{Plan: wire.Plan(plan)}, nil
}

// CommentPlan implements cliv1.DaemonServiceServer.
func (s *Server) CommentPlan(ctx context.Context, req *cliv1.CommentPlanRequest) (*cliv1.CommentPlanResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	plan, err := s.app.CommentPlan(ctx, wire.ToCallContext(req.GetContext()), core.PlanNumber(req.GetNumber()), req.GetText())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.CommentPlanResponse{Plan: wire.Plan(plan)}, nil
}

// ClosePlan implements cliv1.DaemonServiceServer.
func (s *Server) ClosePlan(ctx context.Context, req *cliv1.ClosePlanRequest) (*cliv1.ClosePlanResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	status, err := wire.ToPlanStatus("status", req.GetStatus())
	if err != nil {
		return nil, wire.Error(err)
	}
	plan, err := s.app.ClosePlan(ctx, wire.ToCallContext(req.GetContext()), core.PlanNumber(req.GetNumber()), status, req.GetComment())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ClosePlanResponse{Plan: wire.Plan(plan)}, nil
}
