package client

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DescribeTask asks the daemon for the card of a task, or why the task has none.
func (c *Client) DescribeTask(ctx context.Context, req *cliv1.DescribeTaskRequest) (*cliv1.DescribeTaskResponse, error) {
	resp, err := c.rpc.DescribeTask(ctx, req)
	return resp, c.fail(err)
}
