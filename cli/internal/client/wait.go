package client

import (
	"context"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
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

// Wait blocks on the jobs. It is done over the daemon's watch stream, from the states it reports,
// so the answer is the same for every caller who waits on a shared job.
func (c *Client) Wait(ctx context.Context, callCtx *cliv1.CallContext, ids []string, opts WaitOptions) (WaitResult, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	var timedOut chan struct{}
	if opts.Timeout > 0 {
		fired, stop := c.opts.Timers.Timeout(opts.Timeout)
		defer stop()
		timedOut = make(chan struct{})
		go func() {
			select {
			case <-fired:
				close(timedOut)
				cancel()
			case <-ctx.Done():
			}
		}()
	}

	expired := func() bool {
		if timedOut == nil {
			return false
		}
		select {
		case <-timedOut:
			return true
		default:
			return false
		}
	}
	stream, err := c.rpc.Watch(ctx, &cliv1.WatchRequest{Context: callCtx, JobIds: ids})
	if err != nil {
		if expired() {
			return WaitResult{TimedOut: true}, nil
		}
		return WaitResult{}, c.fail(err)
	}
	latest := map[string]*cliv1.Job{}
	result := func() WaitResult {
		jobs := make([]*cliv1.Job, 0, len(ids))
		for _, id := range ids {
			if j, ok := latest[id]; ok {
				jobs = append(jobs, j)
			}
		}
		return WaitResult{Jobs: jobs}
	}
	for {
		resp, err := stream.Recv()
		if err != nil {
			r := result()
			if expired() {
				r.TimedOut = true
				return r, nil
			}
			if errEnd := streamEnd(c, err); errEnd != nil {
				return r, errEnd
			}
			// The daemon ends the stream once every job is final.
			r.Done, r.Succeeded = wire.Await(r.Jobs, false)
			return r, nil
		}
		latest[resp.GetJob().GetId()] = resp.GetJob()
		r := result()
		if len(r.Jobs) == len(ids) {
			if done, ok := wire.Await(r.Jobs, opts.Any); done {
				r.Done, r.Succeeded = true, ok
				return r, nil
			}
		}
	}
}
