package server_test

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"google.golang.org/protobuf/proto"
)

const watchLease = 30 * time.Second

// Enumerate the boot gap at every probe in the lease, both for an existing
// subscription and one opened after the successor published its epoch.
func TestWatchRunsSuccessorBootGapProperty(t *testing.T) {
	for _, outcome := range []storev1.Outcome{storev1.Outcome_OUTCOME_FINISHED, storev1.Outcome_OUTCOME_STOPPED, storev1.Outcome_OUTCOME_FAILED, storev1.Outcome_OUTCOME_INTERRUPTED} {
		for gap := 0; gap < int(watchLease/server.LivenessProbeInterval); gap++ {
			for _, fresh := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/gap-%d/fresh-%t", outcome, gap, fresh), func(t *testing.T) {
					ticker := &fakeTicker{ticks: make(chan time.Time, 1), stopped: make(chan struct{})}
					clock := &fakeClock{ticker: ticker}
					f := startWithClock(t, clock)
					// An existing free inode makes flock acquisition invisible to fsnotify.
					lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
					if err != nil {
						t.Fatal(err)
					}
					defer lock.Close()
					predecessor := head(proto.Uint64(0))
					predecessor.Epoch = 7
					predecessor.Lifecycle = &storev1.Lifecycle{State: &storev1.Lifecycle_Ended{Ended: &storev1.Ended{Outcome: outcome}}}
					if outcome == storev1.Outcome_OUTCOME_FAILED {
						predecessor.GetLifecycle().GetEnded().Failure = &storev1.RunFailure{Kind: storev1.RunFailureKind_RUN_FAILURE_KIND_CONFIGURATION}
					}
					publishHead(t, f, predecessor)
					ctx, cancel := context.WithCancel(f.ctx)
					defer cancel()
					c := client(t, f, false)
					var stream apiv1.HostService_WatchRunsClient
					if !fresh {
						stream, err = c.WatchRuns(ctx, &apiv1.WatchRunsRequest{})
						if err != nil {
							t.Fatal(err)
						}
						if got := watchFrame(t, stream); got.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ENDED {
							t.Fatal(got)
						}
						if !watchFrame(t, stream).InitialComplete {
							t.Fatal("missing marker")
						}
					}
					successor := head(proto.Uint64(0))
					successor.Epoch = 8
					successor.Lifecycle = &storev1.Lifecycle{State: &storev1.Lifecycle_Starting{Starting: &storev1.Starting{Stage: storev1.StartupStage_STARTUP_STAGE_PROBING_WORKSPACE, State: storev1.StageState_STAGE_STATE_RUNNING}}}
					if gap%2 == 1 {
						successor.Lifecycle = nil
						successor.AwaitingLifecycle = true
					}
					publishHead(t, f, successor)
					if fresh {
						stream, err = c.WatchRuns(ctx, &apiv1.WatchRunsRequest{})
						if err != nil {
							t.Fatal(err)
						}
					}
					for {
						got := watchFrame(t, stream)
						if got.Head.GetEpoch() == 8 {
							assertSuccessor(t, got)
							break
						}
					}
					if fresh && !watchFrame(t, stream).InitialComplete {
						t.Fatal("missing marker")
					}
					for tick := 0; tick <= gap; tick++ {
						if tick == gap {
							if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
								t.Fatal(err)
							}
						}
						probeTick(clock, ticker, time.Duration(tick+1)*server.LivenessProbeInterval)
						alive := false
						for {
							got := watchFrame(t, stream)
							if got.Heartbeat {
								break
							}
							assertSuccessor(t, got)
							alive = alive || got.Liveness == apiv1.RunLiveness_RUN_LIVENESS_ALIVE
						}
						if tick == gap && !alive {
							t.Fatal("successor writer was not re-probed after the boot gap")
						}
					}
				})
			}
		}
	}
}

func assertSuccessor(t *testing.T, got *apiv1.WatchRunsResponse) {
	t.Helper()
	if got.Head.GetEpoch() != 8 || (!got.Head.GetAwaitingLifecycle() && got.Head.GetLifecycle().GetStarting() == nil) || got.Head.GetLifecycle().GetEnded() != nil || got.Liveness == apiv1.RunLiveness_RUN_LIVENESS_ENDED {
		t.Fatalf("successor streamed predecessor outcome: %v", got)
	}
}

func TestWatchRunsUnconfirmedProbeExpiresWithLease(t *testing.T) {
	ticker := &fakeTicker{ticks: make(chan time.Time, 1), stopped: make(chan struct{})}
	clock := &fakeClock{ticker: ticker}
	f := startWithClock(t, clock)
	lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	pending := head(proto.Uint64(0))
	pending.Epoch = 8
	publishHead(t, f, pending)
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	stream, err := client(t, f, false).WatchRuns(ctx, &apiv1.WatchRunsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	watchFrame(t, stream)
	if !watchFrame(t, stream).InitialComplete {
		t.Fatal("missing marker")
	}
	probeTick(clock, ticker, watchLease+time.Nanosecond)
	if !watchFrame(t, stream).Heartbeat {
		t.Fatal("expected heartbeat at expiration")
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	probeTick(clock, ticker, watchLease+server.LivenessProbeInterval)
	if got := watchFrame(t, stream); !got.Heartbeat {
		t.Fatalf("expired probe was renewed: %v", got)
	}
}

func watchFrame(t *testing.T, stream apiv1.HostService_WatchRunsClient) *apiv1.WatchRunsResponse {
	t.Helper()
	got, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	return got
}

func probeTick(clock *fakeClock, ticker *fakeTicker, elapsed time.Duration) {
	clock.now.Store(int64(elapsed))
	ticker.ticks <- time.Time{}.Add(elapsed)
}
