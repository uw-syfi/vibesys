package client

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// CreatePlan stores a new plan.
func (c *Client) CreatePlan(ctx context.Context, req *cliv1.CreatePlanRequest) (*cliv1.CreatePlanResponse, error) {
	resp, err := c.rpc.CreatePlan(ctx, req)
	return resp, c.fail(err)
}

// ListPlans lists the plans of a task.
func (c *Client) ListPlans(ctx context.Context, req *cliv1.ListPlansRequest) (*cliv1.ListPlansResponse, error) {
	resp, err := c.rpc.ListPlans(ctx, req)
	return resp, c.fail(err)
}

// ViewPlan returns a plan with its comments and the jobs linked to it.
func (c *Client) ViewPlan(ctx context.Context, req *cliv1.ViewPlanRequest) (*cliv1.ViewPlanResponse, error) {
	resp, err := c.rpc.ViewPlan(ctx, req)
	return resp, c.fail(err)
}

// EditPlan changes a plan's title, body or status.
func (c *Client) EditPlan(ctx context.Context, req *cliv1.EditPlanRequest) (*cliv1.EditPlanResponse, error) {
	resp, err := c.rpc.EditPlan(ctx, req)
	return resp, c.fail(err)
}

// CommentPlan adds a comment to a plan.
func (c *Client) CommentPlan(ctx context.Context, req *cliv1.CommentPlanRequest) (*cliv1.CommentPlanResponse, error) {
	resp, err := c.rpc.CommentPlan(ctx, req)
	return resp, c.fail(err)
}

// ClosePlan sets a status other than open on a plan and adds a comment.
func (c *Client) ClosePlan(ctx context.Context, req *cliv1.ClosePlanRequest) (*cliv1.ClosePlanResponse, error) {
	resp, err := c.rpc.ClosePlan(ctx, req)
	return resp, c.fail(err)
}
