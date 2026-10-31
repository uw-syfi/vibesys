package server_test

import (
	"context"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
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
	// ended is what the source returned if it stopped without being asked to.
	ended chan error
	// alive counts the signs of life the source gave beyond its values.
	alive atomic.Int64
}

func newRecordedRuns() *recordedRuns {
	return &recordedRuns{puts: make(chan putRun, 1024), removed: make(chan string, 1024), synced: make(chan struct{}, 1), ended: make(chan error, 1)}
}

func (r *recordedRuns) Alive()                                { r.alive.Add(1) }
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
	case err := <-r.ended:
		t.Fatalf("the source ended and put no value: %v", err)
		return putRun{}
	case <-ctx.Done():
		t.Fatal("the source put no value")
		return putRun{}
	}
}

func (r *recordedRuns) awaitSynced(t *testing.T, ctx context.Context) {
	t.Helper()
	select {
	case <-r.synced:
	case err := <-r.ended:
		t.Fatalf("the source ended before its snapshot was complete: %v", err)
	case <-ctx.Done():
		t.Fatal("the source did not complete its snapshot")
	}
}

// stepTicker is a ticker a test steps one tick at a time and knows the end of. A source asks for
// its ticks (`Ticks`) each time it waits for its next event, so it has finished a tick exactly
// when it asks again. Each ask has a channel of its own, so the tick a test hands over is taken by
// the one ask the test saw, and the next ask is known to be after it.
type stepTicker struct {
	mu      sync.Mutex
	asked   int
	current chan time.Time
	// changed wakes a test that waits for an ask. It carries no count: the test reads the count.
	changed chan struct{}
	stopped chan struct{}
}

func newStepTicker() *stepTicker {
	return &stepTicker{changed: make(chan struct{}, 1), stopped: make(chan struct{})}
}

func (s *stepTicker) Ticks() <-chan time.Time {
	s.mu.Lock()
	s.asked++
	s.current = make(chan time.Time)
	ticks := s.current
	s.mu.Unlock()
	select {
	case s.changed <- struct{}{}:
	default:
	}
	return ticks
}

func (s *stepTicker) Stop() { close(s.stopped) }

func (s *stepTicker) latest() (int, chan time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.asked, s.current
}

// tick hands the source a tick and returns true once the source has finished it. It returns false
// if `ctx` ends first: the source took no tick, or never came back for the next. A source busy with
// something else is not asking yet, and one that asks and then leaves its wait (an event came
// first) asks again, so the tick waits for the ask that is there.
func (s *stepTicker) tick(ctx context.Context, at time.Time) bool {
	for {
		ask, ticks := s.latest()
		select {
		case ticks <- at:
			return s.awaitAskAfter(ctx, ask)
		case <-s.changed:
		case <-ctx.Done():
			return false
		}
	}
}

func (s *stepTicker) awaitAskAfter(ctx context.Context, ask int) bool {
	for {
		if asked, _ := s.latest(); asked > ask {
			return true
		}
		select {
		case <-s.changed:
		case <-ctx.Done():
			return false
		}
	}
}

// runsWorld is one published run on a root whose liveness probes the test drives, and the
// source reading it. A probe tick is handed over, and `probe` returns when the source has
// finished it, so what a tick caused is complete when probe returns and nothing of the source
// is still running beside the test.
type runsWorld struct {
	t      *testing.T
	f      fixture
	ctx    context.Context
	feed   *server.ScriptedFeed
	ticker *stepTicker
	clock  *fakeClock
	sink   *recordedRuns
	lock   *os.File
}

func newRunsWorld(t *testing.T) *runsWorld {
	t.Helper()
	ticker := newStepTicker()
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
	go func() {
		err := source.Run(ctx, w.sink)
		if ctx.Err() == nil {
			w.sink.ended <- err
		}
		done <- err
	}()
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
	if !w.ticker.tick(w.ctx, time.Time{}.Add(elapsed)) {
		w.t.Fatal("the source did not finish a probe tick")
	}
}

// idle returns once the source is waiting for its next event: it has scanned and asked for a tick.
func (w *runsWorld) idle() {
	w.t.Helper()
	if !w.ticker.awaitAskAfter(w.ctx, 0) {
		w.t.Fatal("the source did not come to wait for an event")
	}
}

// change tells the source a watched directory changed, as a platform watcher does, and returns
// once the source has scanned again. The source must be idle: it is, after idle, probe and itself.
func (w *runsWorld) change() {
	w.t.Helper()
	before, _ := w.ticker.latest()
	w.feed.Change()
	if !w.ticker.awaitAskAfter(w.ctx, before) {
		w.t.Fatal("the source did not scan after a change")
	}
}

// takeWriterLock locks the run as its writer does, and fails if the lock is not free. The source
// takes it shared for an instant to probe it, but only while it handles a tick, and `probe`
// returns when it has finished one, so the lock is free here and a refusal is a harness fault.
func (w *runsWorld) takeWriterLock() {
	w.t.Helper()
	if err := syscall.Flock(int(w.lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		w.t.Fatalf("the writer lock is held while the source is idle: %v", err)
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

// A host with no watches left for some of its runs still lists every run and follows each one's
// head: a change of a run that is watched is heard at once, and one of a run that is not is found
// within a probe tick. Whatever the budget, no run is missing and none is left old.
func TestRunsSourceListsAndFollowsEveryRunWhateverItsWatchBudget(t *testing.T) {
	const runs = 5
	// The root, the run index and the address directory are watched first, and a host that cannot
	// watch those has no source at all (the contract holds that case to a failed topic).
	const fixed = 3
	sawWatched, sawPolled := false, false
	for budget := fixed; budget <= fixed+runs+1; budget++ {
		t.Run(fmt.Sprintf("budget-%d", budget), func(t *testing.T) {
			w := newRunsWorld(t)
			w.feed.Budget = budget
			dirs := map[string]string{}
			for index := range runs {
				id := fmt.Sprintf("r%d", index)
				project := addProjectRun(t, w, id)
				dir, err := filepath.EvalSymlinks(filepath.Join(project, ".vibesys", "state", "local", "runs", id, "logs"))
				if err != nil {
					t.Fatal(err)
				}
				dirs[id] = dir
				publishHeadIn(t, dir, head(proto.Uint64(1)))
			}
			defer w.run()()
			w.sink.awaitSynced(t, w.ctx)
			w.idle()
			state := map[string]uint64{}
			take := func() {
				for _, put := range w.sink.drain() {
					state[put.id] = put.entry.GetHead().GetCommittedSeq()
				}
			}
			take()
			for id := range dirs {
				if seq, listed := state[id]; !listed || seq != 1 {
					t.Fatalf("%s is listed at %d (listed %t) before any change", id, seq, listed)
				}
			}
			watched := w.feed.WatchList()
			elapsed := time.Duration(0)
			for id, dir := range dirs {
				publishHeadIn(t, dir, head(proto.Uint64(2)))
				if slices.Contains(watched, dir) {
					sawWatched = true
					w.change()
				} else {
					sawPolled = true
					// Nothing tells the source of this change, and it is idle: it has seen none.
					take()
					if state[id] != 1 {
						t.Fatalf("%s changed with no watch and the source heard of it", id)
					}
					elapsed += server.LivenessProbeInterval
					w.probe(elapsed)
				}
				take()
				if state[id] != 2 {
					t.Fatalf("%s is at %d after its change, with budget %d", id, state[id], budget)
				}
			}
			for id := range dirs {
				if state[id] != 2 {
					t.Fatalf("%s was left at %d", id, state[id])
				}
			}
		})
	}
	if !sawWatched || !sawPolled {
		t.Fatalf("the budgets did not cover both a watched and an unwatched run (watched %t, unwatched %t)", sawWatched, sawPolled)
	}
}

// The runs topic holds its source to a lease, and the source says it is alive on every run it
// reads and on every probe tick, so a long scan or a quiet index does not end it. The count of
// signs is what the hub's watchdog reads: it grows with each run of a scan and with each tick.
func TestRunsSourceGivesASignOfLifeOnEveryRunItReadsAndEveryTick(t *testing.T) {
	if hostoperations.Topics[hostoperations.TopicRuns].SourceLease <= 0 {
		t.Fatal("the runs topic holds its source to no lease")
	}
	for runs := 1; runs <= 6; runs++ {
		t.Run(fmt.Sprintf("%d-runs", runs), func(t *testing.T) {
			w := newRunsWorld(t)
			for index := range runs {
				addProjectRun(t, w, fmt.Sprintf("r%d", index))
			}
			defer w.run()()
			w.sink.awaitSynced(t, w.ctx)
			w.idle()
			// The world's own run is read too: every run of the index is a sign.
			if got := w.sink.alive.Load(); got < int64(runs) {
				t.Fatalf("%d signs after reading %d runs", got, runs)
			}
			for tick := 1; tick <= 3; tick++ {
				before := w.sink.alive.Load()
				w.probe(time.Duration(tick) * server.LivenessProbeInterval)
				if w.sink.alive.Load() <= before {
					t.Fatalf("a probe tick gave no sign of life")
				}
			}
		})
	}
}
