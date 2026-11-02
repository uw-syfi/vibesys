package daemon

import (
	"context"
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// The calls below belong to commands that arrive with the environments and the result flows. They
// are refused as unavailable and say so, so a client of this build is told, and not sent a
// generic "unimplemented" status it cannot read.

func notInThisBuild(call string) error {
	return wire.Error(&core.Failure{
		Kind:    core.FailureUnavailable,
		Message: fmt.Sprintf("%s is not available in this build of the daemon", call),
	})
}

// CreatePlan implements cliv1.DaemonServiceServer.
func (*Server) CreatePlan(context.Context, *cliv1.CreatePlanRequest) (*cliv1.CreatePlanResponse, error) {
	return nil, notInThisBuild("CreatePlan")
}

// ListPlans implements cliv1.DaemonServiceServer.
func (*Server) ListPlans(context.Context, *cliv1.ListPlansRequest) (*cliv1.ListPlansResponse, error) {
	return nil, notInThisBuild("ListPlans")
}

// ViewPlan implements cliv1.DaemonServiceServer.
func (*Server) ViewPlan(context.Context, *cliv1.ViewPlanRequest) (*cliv1.ViewPlanResponse, error) {
	return nil, notInThisBuild("ViewPlan")
}

// EditPlan implements cliv1.DaemonServiceServer.
func (*Server) EditPlan(context.Context, *cliv1.EditPlanRequest) (*cliv1.EditPlanResponse, error) {
	return nil, notInThisBuild("EditPlan")
}

// CommentPlan implements cliv1.DaemonServiceServer.
func (*Server) CommentPlan(context.Context, *cliv1.CommentPlanRequest) (*cliv1.CommentPlanResponse, error) {
	return nil, notInThisBuild("CommentPlan")
}

// ClosePlan implements cliv1.DaemonServiceServer.
func (*Server) ClosePlan(context.Context, *cliv1.ClosePlanRequest) (*cliv1.ClosePlanResponse, error) {
	return nil, notInThisBuild("ClosePlan")
}
