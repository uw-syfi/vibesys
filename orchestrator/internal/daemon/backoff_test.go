package daemon_test

import (
	"context"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// failures counts the warnings that say a step failed.
type failures struct{ n atomic.Int64 }

func (*failures) Enabled(context.Context, slog.Level) bool { return true }
func (f *failures) Handle(_ context.Context, r slog.Record) error {
	if r.Level == slog.LevelWarn {
		f.n.Add(1)
	}
	return nil
}
func (f *failures) WithAttrs([]slog.Attr) slog.Handler { return f }
func (f *failures) WithGroup(string) slog.Handler      { return f }

// A session whose sidecar is gone but not yet known to be gone fails every command sent to it. The
// loop asks again on later ticks, but each failure in a row waits longer than the one before, so the
// failure is not repeated at the rate of the tick for as long as it lasts.
func TestAStepThatKeepsFailingIsTriedLessOftenEachTime(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Sidecars.SendError = status.Error(codes.Unavailable, "dial unix: no such file or directory")
	count := &failures{}
	ctx, cancel := context.WithCancel(context.Background())
	ticks := make(chan time.Time)
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.Daemon.Run(ctx, ticks, r.Wake, slog.New(count))
	}()
	dispatch(t, r, startRun("start-1"))

	const total = 200
	for range total {
		ticks <- time.Time{}
	}
	ticks <- time.Time{} // taken only once the step before it has finished.
	cancel()
	select {
	case <-done:
	case <-time.After(30 * time.Second): // guards against a hang only.
		t.Fatal("the loop did not stop")
	}

	// Without a backoff every one of the ticks fails, and the doubling alone gives about a dozen.
	if got := count.n.Load(); got < 3 || got > 20 {
		t.Fatalf("%d failed steps over %d ticks, want a few that thin out (at most %d ticks apart)", got, total, daemon.MaxBackoffTicks)
	}
}
