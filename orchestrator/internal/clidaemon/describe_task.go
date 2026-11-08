package clidaemon

import (
	"context"

	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// DescribeTask asks the daemon for the card of a task, or why its files cannot be read. The daemon is
// the only reader of task files; the caller's context bounds the call.
func (c *Client) DescribeTask(ctx context.Context, r *pb.DescribeTaskRequest) (*pb.DescribeTaskResponse, error) {
	return c.svc.DescribeTask(ctx, r)
}
