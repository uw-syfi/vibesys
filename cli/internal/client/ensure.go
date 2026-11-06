package client

import (
	"context"
	"errors"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Ensure bounds discovery, startup ownership, every handshake and RPC, child
// readiness and the final status under one timer. Timeout is typed unavailable.
func Ensure(ctx context.Context, opts Options) (*cliv1.StatusResponse, error) {
	opts = opts.withDefaults()
	ctx, done := clock.Deadline(ctx, opts.Timers, 2*time.Minute)
	defer done()
	if opts.SetUp != nil {
		if err := opts.SetUp(); err != nil {
			return nil, err
		}
	}
	c, err := Connect(ctx, opts)
	var status *cliv1.StatusResponse
	if err == nil {
		defer c.Close()
		status, err = c.Status(ctx)
	}
	if errors.Is(context.Cause(ctx), context.DeadlineExceeded) {
		return nil, wire.Unavailable("daemon ensure timed out")
	}
	return status, err
}
