package client

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// StartWorkstream starts a workstream, or finds the one that is not closed with the same title.
func (c *Client) StartWorkstream(ctx context.Context, req *cliv1.StartWorkstreamRequest) (*cliv1.StartWorkstreamResponse, error) {
	resp, err := c.rpc.StartWorkstream(ctx, req)
	return resp, c.fail(err)
}

// ListWorkstreams lists the workstreams of a task.
func (c *Client) ListWorkstreams(ctx context.Context, req *cliv1.ListWorkstreamsRequest) (*cliv1.ListWorkstreamsResponse, error) {
	resp, err := c.rpc.ListWorkstreams(ctx, req)
	return resp, c.fail(err)
}

// EditWorkstream changes a workstream's title, status or note.
func (c *Client) EditWorkstream(ctx context.Context, req *cliv1.EditWorkstreamRequest) (*cliv1.EditWorkstreamResponse, error) {
	resp, err := c.rpc.EditWorkstream(ctx, req)
	return resp, c.fail(err)
}
