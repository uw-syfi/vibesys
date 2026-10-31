package server_test

import (
	"context"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
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

// takeWriterLock locks the run as its writer does. The source probes the lock by taking it shared
// for an instant, and the probe of the last tick `probe` sent may still be running when it
// returns, so a writer that did not wait would be refused by a probe that is about to let go. It
// waits: what it ends up holding does not depend on which came first.
func (w *runsWorld) takeWriterLock() {
	w.t.Helper()
	for {
		err := syscall.Flock(int(w.lock.Fd()), syscall.LOCK_EX)
		if err == nil {
			return
		}
		if err != syscall.EINTR {
			w.t.Fatal(err)
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
							w.takeWriterLock()
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
	w.takeWriterLock()
	w.probe(watchLease + server.LivenessProbeInterval)
	if got := w.sink.drain(); len(got) != 0 {
		t.Fatalf("expired probe was renewed: %v", got)
	}
}

// A run whose head is past the topic's bound is listed without its head: it never fails the
// topic for the runs beside it. Every put fits the bound, a head is dropped exactly when the
// entry the source would otherwise state is past it (the run's project counts), and what the
// run says of whether it is alive does not depend on whether its head was kept.
func TestRunsSourceListsEveryRunWhateverItsHeadSize(t *testing.T) {
	maxBytes := hostoperations.Topics[hostoperations.TopicRuns].MaxValueBytes
	for seed := int64(1); seed <= 8; seed++ {
		t.Run(fmt.Sprintf("seed-%d", seed), func(t *testing.T) {
			random := rand.New(rand.NewSource(seed))
			w := newRunsWorld(t)
			written := map[string]*storev1.RunHead{}
			projects := map[string]string{}
			for index := range 3 + random.Intn(4) {
				id := fmt.Sprintf("sized-%d", index)
				project := addProjectRun(t, w, id)
				projects[id] = project
				written[id] = headFittingEntry(random, project, maxBytes+random.Intn(2*len(project)+40)-len(project)-20)
				if random.Intn(8) == 0 {
					// Past what a head is read to at all: still a listed run.
					written[id] = headFittingEntry(random, project, 1<<20+1024)
				}
				publishHeadIn(t, filepath.Join(w.f.root, "runs", id), written[id])
			}
			defer w.run()()
			w.sink.awaitSynced(t, w.ctx)
			got := map[string]*apiv1.RunEntry{}
			for _, put := range w.sink.drain() {
				got[put.id] = put.entry
			}
			if _, listed := got["run-1"]; !listed {
				t.Fatal("the run with no head is not listed")
			}
			for id, head := range written {
				entry, listed := got[id]
				if !listed {
					t.Fatalf("%s is not listed", id)
				}
				if size := proto.Size(entry); size > maxBytes {
					t.Fatalf("%s is stated at %d bytes, past the topic's bound of %d", id, size, maxBytes)
				}
				if entry.GetProjectRoot() != projects[id] {
					t.Fatalf("%s lost its project: %q", id, entry.GetProjectRoot())
				}
				whole := proto.Size(&apiv1.RunEntry{Head: head, Liveness: entry.Liveness, ProjectRoot: entry.ProjectRoot})
				if kept := entry.Head != nil; kept == (whole > maxBytes) {
					t.Fatalf("%s: whole entry is %d bytes of a bound of %d, and its head kept is %t", id, whole, maxBytes, kept)
				}
				// The head says it ended; so does the run, whether or not its head is stated, if it
				// could be read at all.
				if proto.Size(head) <= 1<<20 && entry.Liveness != apiv1.RunLiveness_RUN_LIVENESS_ENDED {
					t.Fatalf("%s: an ended run is %v", id, entry.Liveness)
				}
			}
		})
	}
}

// addProjectRun makes a run `id` in the layout a project's run has, so the source can tell its
// project, links it into the index, and returns the project's canonical path.
func addProjectRun(t *testing.T, w *runsWorld, id string) string {
	t.Helper()
	project := filepath.Join(t.TempDir(), "project-"+id)
	logs := filepath.Join(project, ".vibesys", "state", "local", "runs", id, "logs")
	if err := os.MkdirAll(logs, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(logs, filepath.Join(w.f.root, "runs", id)); err != nil {
		t.Fatal(err)
	}
	canonical, err := filepath.EvalSymlinks(project)
	if err != nil {
		t.Fatal(err)
	}
	return canonical
}

// headFittingEntry is an ended run's head whose entry, with its project and an ended liveness,
// is as near `target` bytes as a head of filler can be made without passing it.
func headFittingEntry(random *rand.Rand, project string, target int) *storev1.RunHead {
	// Filler keys of 60 000 bytes make most of the head, and one last key is cut to fit the rest.
	fillers := max(0, (target-65_536+60_007)/60_008)
	headOf := func(last int) *storev1.RunHead {
		counts := map[string]uint64{}
		for filler := range fillers {
			counts[strings.Repeat(string(rune('a'+filler%26))+string(rune('a'+filler/26)), 30_000)] = 1
		}
		if last > 0 {
			counts[strings.Repeat("z", last)] = 1
		}
		return &storev1.RunHead{
			Lifecycle:  &storev1.Lifecycle{State: &storev1.Lifecycle_Ended{Ended: &storev1.Ended{Outcome: storev1.Outcome_OUTCOME_FINISHED}}},
			Strategy:   "example",
			SpanCounts: counts,
		}
	}
	size := func(last int) int {
		return proto.Size(&apiv1.RunEntry{Head: headOf(last), Liveness: apiv1.RunLiveness_RUN_LIVENESS_ENDED, ProjectRoot: proto.String(project)})
	}
	// The size grows with the last key, so the longest last key that fits is found by halving.
	low, high := 0, 65_536
	for low < high {
		middle := (low + high + 1) / 2
		if size(middle) <= target {
			low = middle
		} else {
			high = middle - 1
		}
	}
	return headOf(low)
}

// publishHeadIn writes a run's head as the writer does: whole, by rename.
func publishHeadIn(t *testing.T, dir string, value *storev1.RunHead) {
	t.Helper()
	write(t, filepath.Join(dir, "head.tmp"), false, encode(t, value))
	if err := os.Rename(filepath.Join(dir, "head.tmp"), filepath.Join(dir, "run.head")); err != nil {
		t.Fatal(err)
	}
}
