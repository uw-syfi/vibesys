package client

import (
	"context"
	"errors"
	"fmt"
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// WaitOptions says when a wait is over.
type WaitOptions struct {
	// Any ends the wait when one job is final, and not when all are.
	Any bool
	// Timeout ends the wait after this long. Zero waits as long as it takes. The daemon keeps the
	// time and answers TIMED_OUT; a timeout never cancels a job.
	Timeout time.Duration
}

// WaitResult is how a wait ended: the daemon's outcome, as it sent it. The client decides nothing
// about it; a kind it does not know is for the caller to read as no answer.
type WaitResult struct {
	Outcome *cliv1.WaitOutcome
}

// ErrEndUnknown says a wait stopped without learning how its jobs ended. It says nothing about the
// jobs: they may run on and succeed.
var ErrEndUnknown = errors.New("could not learn how the jobs ended")

// How a wait with no timeout goes on when it cannot reach a daemon: about a minute of attempts
// since it last heard anything. A wait with a timeout tries until the timeout.
const (
	reconnectAttempts = 8
	reconnectFirst    = 500 * time.Millisecond
	reconnectLongest  = 30 * time.Second
)

// Wait blocks on the jobs over the daemon's Wait stream, and returns the outcome the daemon
// decided, so the answer is the same for every caller who waits on a shared job.
//
// A stream that breaks says nothing about the jobs: their end is in the store, and the supervisors
// carry on without the daemon. So Wait connects again, which starts a daemon if none runs, and
// waits from there, asking for what is left of the timeout as the client's clock measures it. The
// client's own deadline only bounds the reconnecting: a timeout that passes while no daemon
// answers is ErrEndUnknown, since no daemon said the jobs were not done. So are a failure that is
// not about the input, like a job whose end the daemon cannot record yet, attempts that run out,
// and a stream that ends without an outcome.
func (c *Client) Wait(ctx context.Context, callCtx *cliv1.CallContext, ids []string, opts WaitOptions) (WaitResult, error) {
	clk := c.opts.Clock
	started := clk.Now()
	remaining := func() time.Duration { return opts.Timeout - clk.Now().Sub(started) }
	current := c
	defer func() {
		if current != c {
			current.Close()
		}
	}()
	delay, attempts := reconnectFirst, 0
	for {
		req := &cliv1.WaitRequest{Context: callCtx, JobIds: ids, Any: opts.Any}
		if opts.Timeout > 0 {
			left := remaining()
			if left <= 0 {
				return WaitResult{}, fmt.Errorf("%w: the timeout of %v passed while no daemon answered", ErrEndUnknown, opts.Timeout)
			}
			req.Timeout = durationpb.New(left)
		}
		outcome, heard, err := wait(ctx, current, req)
		if heard {
			delay, attempts = reconnectFirst, 0
		}
		switch {
		case err == nil:
			return WaitResult{Outcome: outcome}, nil
		case ctx.Err() != nil:
			return WaitResult{}, ctx.Err()
		case !errors.Is(err, errBroken):
			return WaitResult{}, err
		}
		if err := c.reconnect(ctx, &current, &delay, &attempts, opts, remaining, err); err != nil {
			return WaitResult{}, err
		}
	}
}

// reconnect connects again after a broken stream, with a backoff, and makes current the new
// connection. It gives up after reconnectAttempts with no timeout, and when the timeout passes
// with one.
func (c *Client) reconnect(ctx context.Context, current **Client, delay *time.Duration, attempts *int, opts WaitOptions, remaining func() time.Duration, err error) error {
	for {
		*attempts++
		if opts.Timeout == 0 && *attempts >= reconnectAttempts {
			return fmt.Errorf("%w: %w", ErrEndUnknown, err)
		}
		step := *delay
		if opts.Timeout > 0 {
			left := remaining()
			if left <= 0 {
				return fmt.Errorf("%w: the timeout of %v passed while no daemon answered: %w", ErrEndUnknown, opts.Timeout, err)
			}
			step = min(step, left)
		}
		if !clock.Sleep(ctx, c.opts.Timers, step) {
			return ctx.Err()
		}
		*delay = min(*delay*2, reconnectLongest)
		next, cerr := Connect(ctx, c.opts)
		if cerr == nil {
			if *current != c {
				(*current).Close()
			}
			*current = next
			return nil
		}
		if wire.IsIncompatible(cerr) {
			// A daemon this client cannot talk to says nothing about the jobs either.
			return fmt.Errorf("%w: %w", ErrEndUnknown, cerr)
		}
		err = fmt.Errorf("%w: %w", errBroken, cerr)
	}
}

// errBroken marks a wait that ended without saying anything about its jobs.
var errBroken = errors.New("the connection to the daemon broke")

// wait follows one Wait stream to its outcome. heard says the stream delivered something.
func wait(ctx context.Context, c *Client, req *cliv1.WaitRequest) (outcome *cliv1.WaitOutcome, heard bool, err error) {
	// Leaving early ends the stream.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := c.rpc.Wait(ctx, req)
	if err != nil {
		return nil, false, classify(c, err)
	}
	for {
		resp, err := stream.Recv()
		if err != nil {
			if end := streamEnd(c, err); end != nil {
				return nil, heard, classify(c, err)
			}
			return nil, heard, fmt.Errorf("%w: the daemon ended the wait without saying how it ended", ErrEndUnknown)
		}
		heard = true
		if o := resp.GetOutcome(); o != nil {
			return o, true, nil
		}
	}
}

// classify says what the end of a wait means for the caller.
func classify(c *Client, err error) error {
	if wire.Broken(err) {
		return fmt.Errorf("%w: %v", errBroken, err)
	}
	failure := c.fail(err)
	if wire.IsNotFound(failure) || wire.IsInvalidInput(failure) {
		return failure
	}
	return fmt.Errorf("%w: %w", ErrEndUnknown, failure)
}
