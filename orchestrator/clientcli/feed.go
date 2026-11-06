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

// openFeed starts the stream and reads its snapshot. The stream lives until ctx ends.
func openFeed(ctx context.Context, c *client.Client) (*feed, *pb.RunsSnapshot, error) {
	stream, err := c.WatchRuns(ctx, 0)
	if err != nil {
		return nil, nil, classify(err)
	}
	f := &feed{stream: stream}
	frame, err := stream.Recv()
	if err != nil {
		return nil, nil, classify(err)
	}
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
	snapshot, event, search := client.DecodeWatchRuns(frame)
	switch {
	case snapshot != nil:
		return v.Load(snapshot), nil
	case event != nil:
		return v.Apply(event), nil
	}
	return v.ApplySearch(search), nil
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
