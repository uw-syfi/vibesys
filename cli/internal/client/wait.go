package client

import (
	"context"
	"errors"
	"fmt"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// WaitOptions says when a wait is over.
type WaitOptions struct {
	// Any ends the wait when one job is final, and not when all are.
	Any bool
	// Timeout ends the wait after this long. Zero waits as long as it takes. A timeout only
	// closes the watch; it never cancels a job.
	Timeout time.Duration
}

// WaitResult is how a wait ended.
type WaitResult struct {
	// Jobs holds the last state seen of each job, in the order they were asked for.
	Jobs []*cliv1.Job
	// Done says the wait ended because the jobs reached the states it waits for.
	Done bool
	// Succeeded says every job that is final succeeded. It is meaningful when Done.
	Succeeded bool
	// TimedOut says the wait ended because the timeout fired first.
	TimedOut bool
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

// Wait blocks on the jobs. It is done over the daemon's watch stream, from the states it reports,
// so the answer is the same for every caller who waits on a shared job.
//
// A stream that breaks says nothing about the jobs: their end is in the store, and the supervisors
// carry on without the daemon. So Wait connects again, which starts a daemon if none runs, and
// watches from there. A failure that is not about the input, like a job whose end the daemon
// cannot record yet, and attempts that run out, are ErrEndUnknown.
func (c *Client) Wait(ctx context.Context, callCtx *cliv1.CallContext, ids []string, opts WaitOptions) (WaitResult, error) {
	caller := ctx
	if opts.Timeout > 0 {
		var stop func()
		ctx, stop = clock.Deadline(ctx, c.opts.Timers, opts.Timeout)
		defer stop()
	}
	w := waiter{ids: ids, any: opts.Any, callCtx: callCtx, latest: map[string]*cliv1.Job{}}
	current := c
	defer func() {
		if current != c {
			current.Close()
		}
	}()
	delay, attempts := reconnectFirst, 0
	for {
		res, heard, err := w.watch(ctx, current)
		if heard {
			delay, attempts = reconnectFirst, 0
		}
		switch {
		case err == nil:
			return res, nil
		case caller.Err() != nil:
			return w.result(), caller.Err()
		case ctx.Err() != nil:
			res := w.result()
			res.TimedOut = true
			return res, nil
		case !errors.Is(err, errBroken):
			return w.result(), err
		}
		for {
			attempts++
			if opts.Timeout == 0 && attempts >= reconnectAttempts {
				return w.result(), fmt.Errorf("%w: %w", ErrEndUnknown, err)
			}
			if !clock.Sleep(ctx, c.opts.Timers, delay) {
				break
			}
			delay = min(delay*2, reconnectLongest)
			next, cerr := Connect(ctx, c.opts)
			if cerr == nil {
				if current != c {
					current.Close()
				}
				current = next
				break
			}
			if wire.IsIncompatible(cerr) {
				return w.result(), cerr
			}
			err = fmt.Errorf("%w: %w", errBroken, cerr)
		}
	}
}

// errBroken marks a watch that ended without saying anything about its jobs.
var errBroken = errors.New("the connection to the daemon broke")

// waiter is what one Wait has seen, which outlives the streams it watches.
type waiter struct {
	ids     []string
	any     bool
	callCtx *cliv1.CallContext
	latest  map[string]*cliv1.Job
}

func (w *waiter) result() WaitResult {
	jobs := make([]*cliv1.Job, 0, len(w.ids))
	for _, id := range w.ids {
		if j, ok := w.latest[id]; ok {
			jobs = append(jobs, j)
		}
	}
	return WaitResult{Jobs: jobs}
}

// watch follows one stream. It returns the result when the jobs got where the wait wants them, and
// heard says the stream delivered something.
func (w *waiter) watch(ctx context.Context, c *Client) (res WaitResult, heard bool, err error) {
	// Leaving early ends the stream.
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := c.rpc.Watch(ctx, &cliv1.WatchRequest{Context: w.callCtx, JobIds: w.ids})
	if err != nil {
		return WaitResult{}, false, classify(c, err)
	}
	for {
		resp, err := stream.Recv()
		if err != nil {
			if end := streamEnd(c, err); end != nil {
				return WaitResult{}, heard, classify(c, err)
			}
			// The daemon ends the stream once every job is final.
			r := w.result()
			r.Done, r.Succeeded = wire.Await(r.Jobs, false)
			return r, heard, nil
		}
		heard = true
		w.latest[resp.GetJob().GetId()] = resp.GetJob()
		if r := w.result(); len(r.Jobs) == len(w.ids) {
			if done, ok := wire.Await(r.Jobs, w.any); done {
				r.Done, r.Succeeded = true, ok
				return r, heard, nil
			}
		}
	}
}

// classify says what the end of a watch means for the wait.
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
