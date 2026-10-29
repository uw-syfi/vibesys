package server_test

import (
	"context"
	"fmt"
	"testing"
	"testing/quick"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
)

// Virtual ticks drive delivery; the fixture context only guards a missing frame.
func TestWatchRunsHeartbeatsOnEveryProbeWithoutFileChanges(t *testing.T) {
	property := func(seed uint8) bool {
		ticker := &fakeTicker{ticks: make(chan time.Time, 1), stopped: make(chan struct{})}
		f := startWithClock(t, &fakeClock{ticker: ticker})
		ctx, cancel := context.WithCancel(f.ctx)
		defer cancel()
		stream, err := client(t, f, false).WatchRuns(ctx, &apiv1.WatchRunsRequest{})
		if err != nil {
			t.Fatal(err)
		}
		watchEntry(t, stream)
		if !watchEntry(t, stream).InitialComplete {
			t.Fatal("missing initial marker")
		}
		for i := 0; i < 1+int(seed%5); i++ {
			ticker.ticks <- time.Time{}
			frame, err := stream.Recv()
			if err != nil {
				t.Fatal(err)
			}
			field := frame.ProtoReflect().Descriptor().Fields().ByName("heartbeat")
			if field == nil || !frame.ProtoReflect().Get(field).Bool() || frame.RunId != "" || frame.Head != nil {
				return false
			}
		}
		cancel()
		select {
		case <-ticker.stopped:
		case <-f.ctx.Done():
			t.Fatal("watch did not release ticker")
		}
		return true
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 8}); err != nil {
		t.Fatal(fmt.Sprint(err))
	}
}
