package client

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DeclareBound records a performance bound an agent states for an objective of the task.
func (c *Client) DeclareBound(ctx context.Context, req *cliv1.DeclareBoundRequest) (*cliv1.DeclareBoundResponse, error) {
	resp, err := c.rpc.DeclareBound(ctx, req)
	return resp, c.fail(err)
}

// ListBounds lists every bound declared for a task, oldest first.
func (c *Client) ListBounds(ctx context.Context, req *cliv1.ListBoundsRequest) (*cliv1.ListBoundsResponse, error) {
	resp, err := c.rpc.ListBounds(ctx, req)
	return resp, c.fail(err)
}
