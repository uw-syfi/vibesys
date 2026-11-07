package production

import (
	"context"
	"testing"
	"testing/synctest"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// test-isolation: exercises the private follower seam of the reconciler; synctest owns scheduling.
//
// A fresh read of a job log is the first view a stream of its own gives, the whole state at the moment
// it opened, and the stream is closed when it has answered, so a read leaves nothing running and
// never returns a later view of the same stream.
func TestAFreshJobLogReadIsTheFirstViewOfAStreamOfItsOwnAndLeavesNothingRunning(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var subs []Subscription
		stopped := 0
		r := &Reconciler{Store: new(search.Store), Follow: func(ctx context.Context, k Subscription, publish func(search.View)) {
			subs = append(subs, k)
			publish(search.View{Reason: core.Readable})
			// A follower streams on until it is stopped, and would publish again.
			publish(search.View{Reason: core.StreamLost})
			<-ctx.Done()
			stopped++
		}}
		run := core.Run{ID: "r", Repository: "/repo", Task: "t", Status: core.RunRunning, JobTool: core.JobToolEndpoint{Executable: "/cli"}}
		for range 5 {
			v, err := r.ReadJobLog(context.Background(), run)
			if err != nil || v.Reason != core.Readable {
				t.Fatalf("read: %v, %v", v.Reason, err)
			}
		}
		synctest.Wait()
		if stopped != 5 || len(subs) != 5 || subs[0] != subscription(run) {
			t.Fatalf("5 reads stopped %d streams after opening %v", stopped, subs)
		}
	})
}

// A read whose stream never answers ends with the context, and leaves nothing running.
func TestAFreshJobLogReadEndsWithItsContext(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := &Reconciler{Store: new(search.Store), Follow: func(ctx context.Context, _ Subscription, _ func(search.View)) { <-ctx.Done() }}
		ctx, cancel := context.WithCancel(context.Background())
		go func() { cancel() }()
		if _, err := r.ReadJobLog(ctx, core.Run{ID: "r"}); err == nil {
			t.Fatal("a read that never answered returned")
		}
	})
}
