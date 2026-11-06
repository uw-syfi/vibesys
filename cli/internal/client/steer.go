package client

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// HoldJob holds a queued job until it is released.
func (c *Client) HoldJob(ctx context.Context, req *cliv1.HoldJobRequest) (*cliv1.HoldJobResponse, error) {
	resp, err := c.rpc.HoldJob(ctx, req)
	return resp, c.fail(err)
}

// ReleaseJob lets a held job start in its turn.
func (c *Client) ReleaseJob(ctx context.Context, req *cliv1.ReleaseJobRequest) (*cliv1.ReleaseJobResponse, error) {
	resp, err := c.rpc.ReleaseJob(ctx, req)
	return resp, c.fail(err)
}

// Drain makes the daemon start no queued job until Resume.
func (c *Client) Drain(ctx context.Context, req *cliv1.DrainRequest) (*cliv1.DrainResponse, error) {
	resp, err := c.rpc.Drain(ctx, req)
	return resp, c.fail(err)
}

// Resume ends a drain.
func (c *Client) Resume(ctx context.Context) (*cliv1.ResumeResponse, error) {
	resp, err := c.rpc.Resume(ctx, &cliv1.ResumeRequest{})
	return resp, c.fail(err)
}
