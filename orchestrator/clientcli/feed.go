package clientcli

import (
	"context"
	"errors"
	"io"
	"sync/atomic"
	"time"

	"google.golang.org/grpc"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// feed is the stream of runs, read one frame at a time.
type feed struct {
	stream grpc.ServerStreamingClient[pb.WatchRunsResponse]
}

// openFeed starts the stream and reads its snapshot, which must come within serviceBound. The stream
// lives on from there until ctx ends, with no bound of its own: a run takes as long as it takes.
func openFeed(ctx context.Context, clock Clock, c *client.Client) (*feed, *pb.RunsSnapshot, error) {
	ctx, cancel := context.WithCancel(ctx)
	timer := clock.After(serviceBound)
	first := make(chan struct{})
	var expired atomic.Bool
	go func() {
		select {
		case <-timer:
			expired.Store(true)
			cancel()
		case <-first:
		case <-ctx.Done():
		}
	}()
	stream, err := c.WatchRuns(ctx, 0)
	var frame *pb.WatchRunsResponse
	if err == nil {
		frame, err = stream.Recv()
	}
	close(first)
	if err != nil {
		cancel()
		if expired.Load() {
			return nil, nil, noAnswer("send the runs", serviceBound)
		}
		return nil, nil, classify(err)
	}
	f := &feed{stream: stream}
	snapshot, _, _ := client.DecodeWatchRuns(frame)
	if snapshot == nil {
		return nil, nil, failed("the orchestrator began a stream of runs with something other than a snapshot")
	}
	return f, snapshot, nil
}

// next folds the next frame into the view and returns its changes. A closed stream is an error:
// no verb waits for the end of a stream.
func (f *feed) next(v *View) ([]Change, error) {
	frame, err := f.stream.Recv()
	if errors.Is(err, io.EOF) {
		return nil, unavailable("the connection to the orchestrator closed")
	}
	if err != nil {
		return nil, classify(err)
	}
	snapshot, change, search := client.DecodeWatchRuns(frame)
	switch {
	case snapshot != nil:
		return v.Load(snapshot), nil
	case change != nil:
		return v.Apply(change), nil
	}
	return v.ApplySearch(search), nil
}

const (
	// handshakeBound is how long a host has to answer the handshake.
	handshakeBound = 10 * time.Second
	// serviceBound is how long the service has to answer one call, or to begin a stream.
	serviceBound = 30 * time.Second
)

// noAnswer is the failure of a service that took too long, with what to do about it.
func noAnswer(what string, d time.Duration) error {
	return timedOut("the host did not %s within %s, so it may be wedged. `%s host status` says more, and `%s host stop --force` ends it, leaving the agents of its runs running until the next host starts", what, d, Program, Program)
}

// within runs one call with an end d from now on the clock, and an end there is a timeout.
func within[T any](ctx context.Context, clock Clock, d time.Duration, what string, call func(context.Context) (T, error)) (T, error) {
	bounded, expired, cancel := bound(ctx, clock, d)
	defer cancel()
	value, err := call(bounded)
	if err != nil && expired() {
		return value, noAnswer(what, d)
	}
	return value, err
}

// bound ends ctx when d has passed on the clock. The returned function says whether it did.
func bound(ctx context.Context, clock Clock, d time.Duration) (context.Context, func() bool, context.CancelFunc) {
	ctx, cancel := context.WithCancel(ctx)
	var expired atomic.Bool
	deadline := clock.After(d)
	go func() {
		select {
		case <-deadline:
			expired.Store(true)
			cancel()
		case <-ctx.Done():
		}
	}()
	return ctx, expired.Load, cancel
}
