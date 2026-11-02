package client

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Show returns the results saved for a commit.
func (c *Client) Show(ctx context.Context, req *cliv1.ShowRequest) (*cliv1.ShowResponse, error) {
	resp, err := c.rpc.Show(ctx, req)
	return resp, c.fail(err)
}

// Compare returns how a commit compares with another, or with the result branch's head.
func (c *Client) Compare(ctx context.Context, req *cliv1.CompareRequest) (*cliv1.CompareResponse, error) {
	resp, err := c.rpc.Compare(ctx, req)
	return resp, c.fail(err)
}

// Promote moves the result branch to a commit. A refusal is the response, not an error.
func (c *Client) Promote(ctx context.Context, req *cliv1.PromoteRequest) (*cliv1.PromoteResponse, error) {
	resp, err := c.rpc.Promote(ctx, req)
	return resp, c.fail(err)
}

// CurrentResult returns the head of the result branch and its final number, or why there is none.
func (c *Client) CurrentResult(ctx context.Context, req *cliv1.CurrentResultRequest) (*cliv1.CurrentResultResponse, error) {
	resp, err := c.rpc.CurrentResult(ctx, req)
	return resp, c.fail(err)
}
