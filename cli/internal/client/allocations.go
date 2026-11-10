package client

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// needAllocations refuses a daemon that predates standing allocations: it would drop what a call
// says of an allocation and answer as if it had not been said.
func (c *Client) needAllocations(what string) error {
	return wire.NeedsAllocations(c.daemon, what)
}

// RequestAllocation asks for a standing allocation of a placement for a run.
func (c *Client) RequestAllocation(ctx context.Context, req *cliv1.RequestAllocationRequest) (*cliv1.RequestAllocationResponse, error) {
	if err := c.needAllocations("an allocation"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.RequestAllocation(ctx, req)
	return resp, c.fail(err)
}

// ListAllocations lists a run's allocations.
func (c *Client) ListAllocations(ctx context.Context, req *cliv1.ListAllocationsRequest) (*cliv1.ListAllocationsResponse, error) {
	if err := c.needAllocations("an allocation"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.ListAllocations(ctx, req)
	return resp, c.fail(err)
}

// ShowAllocation returns one allocation.
func (c *Client) ShowAllocation(ctx context.Context, req *cliv1.ShowAllocationRequest) (*cliv1.ShowAllocationResponse, error) {
	if err := c.needAllocations("an allocation"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.ShowAllocation(ctx, req)
	return resp, c.fail(err)
}

// WaitAllocation waits, for at most the request's timeout, for an allocation to be in a state.
func (c *Client) WaitAllocation(ctx context.Context, req *cliv1.WaitAllocationRequest) (*cliv1.WaitAllocationResponse, error) {
	if err := c.needAllocations("an allocation"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.WaitAllocation(ctx, req)
	return resp, c.fail(err)
}

// EndAllocation ends an allocation of a run.
func (c *Client) EndAllocation(ctx context.Context, req *cliv1.EndAllocationRequest) (*cliv1.EndAllocationResponse, error) {
	if err := c.needAllocations("an allocation"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.EndAllocation(ctx, req)
	return resp, c.fail(err)
}

// RenewAllocation gives an allocation a renewal order, or takes it away.
func (c *Client) RenewAllocation(ctx context.Context, req *cliv1.RenewAllocationRequest) (*cliv1.RenewAllocationResponse, error) {
	if err := c.needAllocations("an allocation"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.RenewAllocation(ctx, req)
	return resp, c.fail(err)
}

// EndRunAllocations ends every allocation of a run that has not ended.
func (c *Client) EndRunAllocations(ctx context.Context, req *cliv1.EndRunAllocationsRequest) (*cliv1.EndRunAllocationsResponse, error) {
	if err := c.needAllocations("an allocation"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.EndRunAllocations(ctx, req)
	return resp, c.fail(err)
}

// ReadPlacementCapacity measures a placement's capacity.
func (c *Client) ReadPlacementCapacity(ctx context.Context, req *cliv1.ReadPlacementCapacityRequest) (*cliv1.ReadPlacementCapacityResponse, error) {
	if err := c.needAllocations("a placement's capacity"); err != nil {
		return nil, err
	}
	resp, err := c.rpc.ReadPlacementCapacity(ctx, req)
	return resp, c.fail(err)
}
