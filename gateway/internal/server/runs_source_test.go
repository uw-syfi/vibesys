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

// putRun is one value the source gave the topic.
type putRun struct {
	id    string
	entry *apiv1.RunEntry
}

// recordedRuns is the sink a source writes to. A call is recorded before it returns,
// so what the source has done is what the test finds, with no clock to wait on.
type recordedRuns struct {
	puts    chan putRun
	removed chan string
	synced  chan struct{}
}

func newRecordedRuns() *recordedRuns {
	return &recordedRuns{puts: make(chan putRun, 1024), removed: make(chan string, 1024), synced: make(chan struct{}, 1)}
}

func (r *recordedRuns) Put(key string, value *apiv1.RunEntry) { r.puts <- putRun{key, value} }
func (r *recordedRuns) Remove(key string)                     { r.removed <- key }
func (r *recordedRuns) Synced() {
	select {
	case r.synced <- struct{}{}:
	default:
	}
}

// drain returns the values put since it was last called, without waiting for more.
func (r *recordedRuns) drain() []putRun {
	var out []putRun
	for {
		select {
		case put := <-r.puts:
			out = append(out, put)
		default:
			return out
		}
	}
}

// next waits for the next value, which the source must produce for the test to go on.
func (r *recordedRuns) next(t *testing.T, ctx context.Context) putRun {
	t.Helper()
	select {
	case put := <-r.puts:
		return put
	case <-ctx.Done():
		t.Fatal("the source put no value")
		return putRun{}
	}
}

func (r *recordedRuns) awaitSynced(t *testing.T, ctx context.Context) {
	t.Helper()
	select {
	case <-r.synced:
	case <-ctx.Done():
		t.Fatal("the source did not complete its snapshot")
	}
}

// runsWorld is one published run on a root whose liveness probes the test drives, and the
// source reading it. A probe tick is not answered until the source has finished the one
// before it, so what a tick caused is complete when probe returns.
type runsWorld struct {
	t      *testing.T
	f      fixture
	ctx    context.Context
	feed   *server.ScriptedFeed
	ticker *fakeTicker
	clock  *fakeClock
	sink   *recordedRuns
	lock   *os.File
}

func newRunsWorld(t *testing.T) *runsWorld {
	t.Helper()
	ticker := &fakeTicker{ticks: make(chan time.Time), stopped: make(chan struct{})}
	clock := &fakeClock{ticker: ticker}
	f := startWithClock(t, clock)
	// An existing free inode makes flock acquisition invisible to a directory watcher.
	lock, err := os.OpenFile(filepath.Join(f.run, "run.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lock.Close() })
	return &runsWorld{t: t, f: f, ctx: f.ctx, feed: server.NewScriptedFeed(), ticker: ticker, clock: clock, sink: newRecordedRuns(), lock: lock}
}

// run starts a source on the world's root and returns what stops it.
func (w *runsWorld) run() (stop func()) {
	ctx, cancel := context.WithCancel(w.ctx)
	done := make(chan error, 1)
	source := server.RunsTopicOver(server.Config{Root: w.f.root, Clock: w.clock, Probes: w.clock}, w.feed).Source
	go func() { done <- source.Run(ctx, w.sink) }()
	return func() {
		cancel()
		<-done
	}
}

// publish writes the run's head, as the writer does, and tells the source the directory changed.
func (w *runsWorld) publish(value *storev1.RunHead) {
	w.t.Helper()
	publishHead(w.t, w.f, value)
	w.feed.Change()
}

// probe ticks the liveness probes at `elapsed` and returns once the source has finished that tick.
func (w *runsWorld) probe(elapsed time.Duration) {
	w.t.Helper()
	w.clock.now.Store(int64(elapsed))
	for range 2 {
		select {
		case w.ticker.ticks <- time.Time{}.Add(elapsed):
		case <-w.ctx.Done():
			w.t.Fatal("the source did not take a probe tick")
		}
	}
}

func succeeds(t *testing.T, got putRun) {
	t.Helper()
	if got.entry.Head.GetEpoch() != 8 || (!got.entry.Head.GetAwaitingLifecycle() && got.entry.Head.GetLifecycle().GetStarting() == nil) ||
		got.entry.Head.GetLifecycle().GetEnded() != nil || got.entry.Liveness == apiv1.RunLiveness_RUN_LIVENESS_ENDED {
		t.Fatalf("successor streamed predecessor outcome: %v", got.entry)
	}
}

// Enumerate the boot gap at every probe in the lease, both for a source already watching
// and one started after the successor published its epoch.
func TestRunsSourceSuccessorBootGapProperty(t *testing.T) {
	for _, outcome := range []storev1.Outcome{storev1.Outcome_OUTCOME_FINISHED, storev1.Outcome_OUTCOME_STOPPED, storev1.Outcome_OUTCOME_FAILED, storev1.Outcome_OUTCOME_INTERRUPTED} {
		for gap := 0; gap < int(watchLease/server.LivenessProbeInterval); gap++ {
			for _, fresh := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/gap-%d/fresh-%t", outcome, gap, fresh), func(t *testing.T) {
					w := newRunsWorld(t)
					predecessor := head(proto.Uint64(0))
					predecessor.Epoch = 7
					predecessor.Lifecycle = &storev1.Lifecycle{State: &storev1.Lifecycle_Ended{Ended: &storev1.Ended{Outcome: outcome}}}
					if outcome == storev1.Outcome_OUTCOME_FAILED {
						predecessor.GetLifecycle().GetEnded().Failure = &storev1.RunFailure{Kind: storev1.RunFailureKind_RUN_FAILURE_KIND_CONFIGURATION}
					}
					publishHead(t, w.f, predecessor)
					successor := head(proto.Uint64(0))
					successor.Epoch = 8
					successor.Lifecycle = &storev1.Lifecycle{State: &storev1.Lifecycle_Starting{Starting: &storev1.Starting{Stage: storev1.StartupStage_STARTUP_STAGE_PROBING_WORKSPACE, State: storev1.StageState_STAGE_STATE_RUNNING}}}
					if gap%2 == 1 {
						successor.Lifecycle = nil
						successor.AwaitingLifecycle = true
					}
					if fresh {
						publishHead(t, w.f, successor)
						defer w.run()()
						w.sink.awaitSynced(t, w.ctx)
					} else {
						defer w.run()()
						w.sink.awaitSynced(t, w.ctx)
						if got := w.sink.next(t, w.ctx); got.entry.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ENDED {
							t.Fatal(got.entry)
						}
						w.publish(successor)
					}
					var seen putRun
					for seen.entry.GetHead().GetEpoch() != 8 {
						seen = w.sink.next(t, w.ctx)
					}
					succeeds(t, seen)
					w.sink.drain()
					for tick := 0; tick <= gap; tick++ {
						if tick == gap {
							if err := syscall.Flock(int(w.lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
								t.Fatal(err)
							}
						}
						w.probe(time.Duration(tick+1) * server.LivenessProbeInterval)
						alive := false
						for _, got := range w.sink.drain() {
							succeeds(t, got)
							alive = alive || got.entry.Liveness == apiv1.RunLiveness_RUN_LIVENESS_ALIVE
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

// A free lock before the epoch's writer was seen alive is probed for one lease and no more:
// a writer that takes the lock after the lease has expired is not looked for.
func TestRunsSourceUnconfirmedProbeExpiresWithLease(t *testing.T) {
	w := newRunsWorld(t)
	pending := head(proto.Uint64(0))
	pending.Epoch = 8
	publishHead(t, w.f, pending)
	defer w.run()()
	w.sink.awaitSynced(t, w.ctx)
	w.sink.drain()
	w.probe(watchLease + time.Nanosecond)
	if got := w.sink.drain(); len(got) != 0 {
		t.Fatalf("a tick past the lease changed a run: %v", got)
	}
	if err := syscall.Flock(int(w.lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	w.probe(watchLease + server.LivenessProbeInterval)
	if got := w.sink.drain(); len(got) != 0 {
		t.Fatalf("expired probe was renewed: %v", got)
	}
}
