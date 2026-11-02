// Package storetest holds what every store.Store must pass: one contract suite, run against the
// Fake and against the file-backed store, with a workload drawn from the real decision core and a
// model that answers every read by brute force.
package storetest

import (
	"errors"
	"fmt"
	"os"
	"reflect"
	"slices"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// Harness is a store under test and the way to restart it.
type Harness struct {
	Store store.Store
	// Reopen closes the store and replaces it with one over the same committed data, as a restart
	// does. Anything claimed and not completed is offered again.
	Reopen func() store.Store
}

// Restart reopens the store.
func (h *Harness) Restart() { h.Store = h.Reopen() }

const (
	seedEnv  = "ORCHESTRATOR_STORE_SEED"
	seedsEnv = "ORCHESTRATOR_STORE_SEEDS"
)

// seeds are the seeds a property runs: a few by default, a range with ORCHESTRATOR_STORE_SEEDS, or
// the one a failure printed.
func seeds(t *testing.T) []uint64 {
	t.Helper()
	if v := os.Getenv(seedEnv); v != "" {
		n, err := strconv.ParseUint(v, 10, 64)
		if err != nil {
			t.Fatalf("%s=%q: %v", seedEnv, v, err)
		}
		return []uint64{n}
	}
	n := 6
	if v := os.Getenv(seedsEnv); v != "" {
		var err error
		if n, err = strconv.Atoi(v); err != nil {
			t.Fatalf("%s=%q: %v", seedsEnv, v, err)
		}
	}
	out := make([]uint64, n)
	for i := range out {
		out[i] = uint64(i + 1)
	}
	return out
}

// failf fails a seed's run and says how to replay it.
func failf(t *testing.T, seed uint64, format string, args ...any) {
	t.Helper()
	t.Fatalf("seed %d: %s\nreplay: %s=%d go test ./internal/store/... -run '%s$'", seed, fmt.Sprintf(format, args...), seedEnv, seed, t.Name())
}

// Contract runs the suite. newHarness gives each test its own empty store.
func Contract(t *testing.T, newHarness func(t *testing.T) *Harness) {
	t.Run("CommitAssignsGapFreeSequences", func(t *testing.T) { commitSequences(t, newHarness(t)) })
	t.Run("EffectsNeedEvents", func(t *testing.T) { effectsNeedEvents(t, newHarness(t)) })
	t.Run("RefusedCommitLeavesNothing", func(t *testing.T) { refusedCommit(t, newHarness(t)) })
	t.Run("PagesHonorTheirCaps", func(t *testing.T) { pageCaps(t, newHarness(t)) })
	t.Run("ReadersAndClaimersRunBesideTheWriter", func(t *testing.T) { concurrent(t, newHarness(t)) })
	t.Run("SnapshotBoundsFinishedRuns", func(t *testing.T) { snapshotBound(t, newHarness(t)) })
	t.Run("TimersWaitForTheirTime", func(t *testing.T) { timers(t, newHarness(t)) })
	t.Run("WorkloadMatchesModel", func(t *testing.T) {
		for _, seed := range seeds(t) {
			workloadMatchesModel(t, seed, newHarness(t))
		}
	})
}

func mustCommit(t *testing.T, s store.Store, r store.Result) store.Committed {
	t.Helper()
	c, err := s.Commit(r)
	if err != nil {
		t.Fatalf("commit: %v", err)
	}
	return c
}

func commitSequences(t *testing.T, h *Harness) {
	w := NewWorkload(1)
	var want uint64
	for range 40 {
		r := w.Next()
		c := mustCommit(t, h.Store, r)
		if c.First != want+1 || c.Last != want+uint64(len(r.Events)) {
			t.Fatalf("committed %d events after %d: got %+v", len(r.Events), want, c)
		}
		want = c.Last
		for i, e := range r.Events {
			if rc, ok := e.Body.(core.Receipt); ok {
				recs, err := h.Store.EventsAfter("", c.First+uint64(i)-1, 1)
				if err != nil || len(recs) != 1 || recs[0].Event.Body.(core.Receipt) != rc || recs[0].Sequence != c.First+uint64(i) {
					t.Fatalf("receipt of %s is not at its own sequence %d: %v %v", rc.Command, c.First+uint64(i), recs, err)
				}
			}
		}
	}
	if c, err := h.Store.Commit(store.Result{}); err != nil || c != (store.Committed{}) {
		t.Fatalf("an empty result must commit nothing: %+v %v", c, err)
	}
	snap, err := h.Store.Snapshot()
	if err != nil || snap.Sequence != want {
		t.Fatalf("snapshot sequence = %d, want %d (%v)", snap.Sequence, want, err)
	}
	h.Restart()
	if snap, err = h.Store.Snapshot(); err != nil || snap.Sequence != want {
		t.Fatalf("after a restart snapshot sequence = %d, want %d (%v)", snap.Sequence, want, err)
	}
	if c := mustCommit(t, h.Store, w.Next()); c.First != want+1 {
		t.Fatalf("the first commit after a restart starts at %d, want %d", c.First, want+1)
	}
}

func effectsNeedEvents(t *testing.T, h *Harness) {
	_, err := h.Store.Commit(store.Result{Effects: []core.Effect{core.CloseSession{Session: core.Session{Agent: "a", N: 1}}}})
	if err == nil {
		t.Fatal("an effect with no event was accepted")
	}
	if claimed, err := h.Store.Claim(farFuture, 10); err != nil || len(claimed) != 0 {
		t.Fatalf("the refused effect reached the outbox: %v %v", claimed, err)
	}
}

// refusedCommit commits something the tables cannot hold, in the middle of a result, and checks that
// nothing of the result is left.
func refusedCommit(t *testing.T, h *Harness) {
	w := NewWorkload(2)
	for range 15 {
		mustCommit(t, h.Store, w.Next())
	}
	before, err := h.Store.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	item := func(id core.ItemID, ordinal uint32) core.Event {
		return core.Event{Run: "run-x", At: at, Body: core.Item{
			ID: id, Turn: "t", Agent: "agent-x", Ordinal: ordinal, Status: core.ItemFinished, At: at, Body: core.Assistant{Text: "hi"},
		}}
	}
	bad := store.Result{
		Events:  []core.Event{item("i-1", 1), item("i-2", 1)},
		Effects: []core.Effect{core.CloseSession{Session: core.Session{Agent: "agent-x", N: 1}}},
	}
	if _, err := h.Store.Commit(bad); err == nil {
		t.Fatal("two items of one agent with one ordinal were accepted")
	}
	undecodable := store.Result{Events: []core.Event{item("i-3", 2), {Run: "run-x", Body: core.Run{ID: "run-x"}}}}
	if _, err := h.Store.Commit(undecodable); err == nil {
		t.Fatal("an event the contract refuses was accepted")
	}
	after, err := h.Store.Snapshot()
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatalf("a refused commit changed the snapshot: %v\nbefore %+v\nafter  %+v", err, before, after)
	}
	if page, err := h.Store.Items("agent-x", 0, 10); err != nil || len(page.Items) != 0 {
		t.Fatalf("a refused commit left items: %+v %v", page, err)
	}
	if err := h.Store.Verify(); err != nil {
		t.Fatalf("verify after refusals: %v", err)
	}
	// The next commit takes the sequence the refused one never used.
	if c := mustCommit(t, h.Store, w.Next()); c.First != before.Sequence+1 {
		t.Fatalf("the commit after a refusal starts at %d, want %d", c.First, before.Sequence+1)
	}
}

// pageCaps checks the item page limits against an agent with many large items.
func pageCaps(t *testing.T, h *Harness) {
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	sizes := func(i int) int {
		switch {
		case i < 100:
			return 10 // 100 small items: the count cap binds
		case i < 140:
			return 100_000 // 40 big ones: the byte cap binds
		default:
			return 1_500_000 // beyond a page on its own
		}
	}
	const total = 143
	for i := 1; i <= total; i++ {
		e := core.Event{Run: "run-x", At: at, Body: core.Item{
			ID: core.ItemID(fmt.Sprintf("i-%d", i)), Turn: "t", Agent: "agent-x", Ordinal: uint32(i), Status: core.ItemFinished, At: at,
			Body: core.Assistant{Text: string(make([]byte, sizes(i)))},
		}}
		mustCommit(t, h.Store, store.Result{Events: []core.Event{e}})
	}
	var seen []uint32
	before := uint32(0)
	for pages := 0; ; pages++ {
		page, err := h.Store.Items("agent-x", before, 0)
		if err != nil {
			t.Fatal(err)
		}
		if len(page.Items) == 0 || len(page.Items) > store.MaxPageItems {
			t.Fatalf("page %d has %d items", pages, len(page.Items))
		}
		bytes := 0
		for i, it := range page.Items {
			if i > 0 && it.Ordinal != page.Items[i-1].Ordinal+1 {
				t.Fatalf("page %d is not contiguous at %d", pages, it.Ordinal)
			}
			bytes += len(it.Body.(core.Assistant).Text)
			seen = append(seen, it.Ordinal)
		}
		if bytes > store.MaxPageBytes && len(page.Items) > 1 {
			t.Fatalf("page %d holds %d bytes in %d items", pages, bytes, len(page.Items))
		}
		if before != 0 && page.Items[len(page.Items)-1].Ordinal != before-1 {
			t.Fatalf("page %d does not end just below %d", pages, before)
		}
		if !page.HasOlder {
			if page.Items[0].Ordinal != 1 {
				t.Fatalf("the last page starts at %d", page.Items[0].Ordinal)
			}
			break
		}
		before = page.Items[0].Ordinal
	}
	if len(seen) != total {
		t.Fatalf("paging saw %d items, want %d", len(seen), total)
	}
	if page, _ := h.Store.Items("agent-x", 0, 5); len(page.Items) != 1 || page.Items[0].Ordinal != total || !page.HasOlder {
		t.Fatalf("the newest page is the one oversize item, alone: %d items", len(page.Items))
	}
	if page, _ := h.Store.Items("agent-x", 11, 5); len(page.Items) != 5 || page.Items[4].Ordinal != 10 {
		t.Fatalf("a small limit must return the five items below 11: %d items", len(page.Items))
	}
}

func timers(t *testing.T, h *Harness) {
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	fire := at.Add(time.Hour)
	event := core.Event{Run: "run-x", At: at, Body: core.Run{
		ID: "run-x", Repository: "r", Task: "t", Status: core.RunRunning, Lead: "agent-x", CreatedAt: at,
	}}
	timer := core.ArmTimer{Kind: core.TimerRunDeadline, Run: "run-x", FireAt: fire}
	launch := core.CloseSession{Session: core.Session{Agent: "agent-x", N: 1}}
	mustCommit(t, h.Store, store.Result{Events: []core.Event{event}, Effects: []core.Effect{timer, launch}})
	if when, ok, err := h.Store.NextDue(); err != nil || !ok || !when.Equal(fire) {
		t.Fatalf("NextDue = %v %v %v, want %v", when, ok, err, fire)
	}
	got, err := h.Store.Claim(at, 10)
	if err != nil || len(got) != 1 || got[0].Effect.ID() != launch.ID() {
		t.Fatalf("before its time only the plain effect is ready: %v %v", got, err)
	}
	h.Restart()
	if got, err := h.Store.Claim(at, 10); err != nil || len(got) != 1 {
		t.Fatalf("a claimed effect must come back after a restart: %v %v", got, err)
	}
	h.Restart()
	got, err = h.Store.Claim(fire, 10)
	if err != nil || len(got) != 2 || got[0].Effect.ID() != timer.ID() || got[1].Effect.ID() != launch.ID() {
		t.Fatalf("at its time the timer is ready, in commit order: %v %v", got, err)
	}
	if !reflect.DeepEqual(got[0].Effect, timer) {
		t.Fatalf("the timer changed in the outbox: %+v", got[0].Effect)
	}
	if _, ok, _ := h.Store.NextDue(); ok {
		t.Fatal("a claimed timer is not due again")
	}
	if err := h.Store.Release(got[0].ID); err != nil {
		t.Fatal(err)
	}
	if when, ok, _ := h.Store.NextDue(); !ok || !when.Equal(fire) {
		t.Fatalf("a released timer is due again: %v %v", when, ok)
	}
	if err := h.Store.Complete(got[1].ID, got[1].ID, 999); err != nil {
		t.Fatalf("completing twice, or an unknown id, must be harmless: %v", err)
	}
	again, _ := h.Store.Claim(fire, 10)
	if len(again) != 1 || again[0].Effect.ID() != timer.ID() {
		t.Fatalf("only the released timer is left: %v", again)
	}
	if err := h.Store.Complete(again[0].ID); err != nil {
		t.Fatal(err)
	}
	h.Restart()
	if left, _ := h.Store.Claim(fire.Add(time.Hour), 10); len(left) != 0 {
		t.Fatalf("completed effects came back: %v", left)
	}
}

// workloadMatchesModel is the main property: whatever a seed's workload does, with restarts,
// claims and compactions between commits, every read equals the model's and the store verifies.
func workloadMatchesModel(t *testing.T, seed uint64, h *Harness) {
	t.Helper()
	w := NewWorkload(seed)
	w.BigOutputs = 10
	m := &model{}
	r := w.r
	pending := map[store.OutboxID]core.EffectID{}
	for step := 1; step <= 150; step++ {
		res := w.Next()
		c, err := h.Store.Commit(res)
		if err != nil {
			failf(t, seed, "step %d: commit: %v", step, err)
		}
		if c.First != m.last()+1 || c.Last != m.last()+uint64(len(res.Events)) {
			failf(t, seed, "step %d: committed %+v after %d", step, c, m.last())
		}
		if err := m.add(res, c); err != nil {
			failf(t, seed, "step %d: %v", step, err)
		}
		switch n := r.IntN(100); {
		case n < 8:
			h.Restart()
			for i := range m.outbox {
				m.outbox[i].claimed = false
			}
			clear(pending)
		case n < 30:
			claimOutbox(t, seed, h, m, pending, 1+r.IntN(4))
		case n < 40:
			completeSome(t, seed, h, m, pending, r.IntN(3) == 0)
		case n < 50:
			compact(t, seed, h, m, w)
		}
		if step%25 == 0 {
			checkAll(t, seed, h, m)
		}
	}
	compact(t, seed, h, m, w)
	checkAll(t, seed, h, m)
	h.Restart()
	checkAll(t, seed, h, m)
}

func claimOutbox(t *testing.T, seed uint64, h *Harness, m *model, pending map[store.OutboxID]core.EffectID, limit int) {
	t.Helper()
	got, err := h.Store.Claim(farFuture, limit)
	if err != nil {
		failf(t, seed, "claim: %v", err)
	}
	var want []core.EffectID
	for i := range m.outbox {
		if !m.outbox[i].claimed && len(want) < limit {
			want = append(want, m.outbox[i].id)
			m.outbox[i].claimed = true
		}
	}
	var ids []core.EffectID
	for _, c := range got {
		ids = append(ids, c.Effect.ID())
		pending[c.ID] = c.Effect.ID()
	}
	if !slices.Equal(ids, want) {
		failf(t, seed, "claimed %v, want %v", ids, want)
	}
}

// completeSome completes every claimed effect, or releases them.
func completeSome(t *testing.T, seed uint64, h *Harness, m *model, pending map[store.OutboxID]core.EffectID, release bool) {
	t.Helper()
	var ids []store.OutboxID
	for id := range pending {
		ids = append(ids, id)
	}
	slices.Sort(ids)
	var err error
	if release {
		err = h.Store.Release(ids...)
		for i := range m.outbox {
			m.outbox[i].claimed = false
		}
	} else {
		err = h.Store.Complete(ids...)
		m.outbox = slices.DeleteFunc(m.outbox, func(e modelEffect) bool { return e.claimed })
	}
	if err != nil {
		failf(t, seed, "settle: %v", err)
	}
	clear(pending)
}

// compact compacts with a random retention and checks recovery and reads are unharmed.
func compact(t *testing.T, seed uint64, h *Harness, m *model, w *Workload) {
	t.Helper()
	p := store.Retention{CheckpointAfter: uint64(w.r.IntN(30)), KeepEvents: time.Duration(w.r.IntN(2)) * time.Hour}
	out, err := h.Store.Compact(p, w.Now())
	if err != nil {
		failf(t, seed, "compact: %v", err)
	}
	if out.Checkpoint > m.last() {
		failf(t, seed, "checkpoint %d is past the log's end %d", out.Checkpoint, m.last())
	}
	checkAll(t, seed, h, m)
}

// checkAll compares every read with the model, recovers, and verifies.
func checkAll(t *testing.T, seed uint64, h *Harness, m *model) {
	t.Helper()
	s := h.Store
	state, err := s.Recover()
	if err != nil {
		failf(t, seed, "recover: %v", err)
	}
	if want := m.state(); !state.Equal(want) {
		failf(t, seed, "recovered state differs from a replay of the log\nrecovered: %+v\nreplayed:  %+v", state, want)
	}
	if v := state.Violations(); len(v) > 0 {
		failf(t, seed, "recovered state breaks %v", v)
	}
	if err := s.Verify(); err != nil {
		failf(t, seed, "verify: %v", err)
	}
	snap, err := s.Snapshot()
	if err != nil || !reflect.DeepEqual(snap, m.snapshot()) {
		failf(t, seed, "snapshot: %v\n got  %+v\n want %+v", err, snap, m.snapshot())
	}
	live := map[core.RunID]bool{}
	for _, r := range snap.Runs {
		live[r.ID] = !r.Status.Final()
	}
	check := func(run core.RunID, after uint64) {
		got, err := s.EventsAfter(run, after, 0)
		if errors.Is(err, store.ErrCompacted) {
			// Only what a finished run or a refused command wrote may be gone, and then no read of a
			// live run is affected.
			if run != "" && live[run] {
				failf(t, seed, "events of live run %s were compacted", run)
			}
			return
		}
		if err != nil {
			failf(t, seed, "events after %d of %q: %v", after, run, err)
		}
		if want := m.eventsAfter(run, after, 0); !reflect.DeepEqual(got, want) && !(len(got) == 0 && len(want) == 0) {
			failf(t, seed, "events after %d of %q:\n got  %d events %v\n want %d events %v", after, run, len(got), seqs(got), len(want), seqs(want))
		}
	}
	for _, run := range append(m.runs(), "") {
		check(run, 0)
		check(run, m.last()/2)
		check(run, m.last())
		if run != "" && live[run] {
			for _, after := range []uint64{0, m.last() / 3, m.last() / 2} {
				got, err := s.EventsAfter(run, after, 7)
				if want := m.eventsAfter(run, after, 7); err != nil || !(len(got) == len(want)) || (len(want) > 0 && got[0].Sequence != want[0].Sequence) {
					failf(t, seed, "limit 7 after %d of %q: %v got %v want %v", after, run, err, seqs(got), seqs(want))
				}
			}
		}
		checkMessages(t, seed, s, m, run)
		checkTurns(t, seed, s, m, run)
	}
	for _, agent := range m.agents() {
		checkItems(t, seed, s, m, agent)
	}
}

func seqs(records []store.EventRecord) []uint64 {
	var out []uint64
	for _, r := range records {
		out = append(out, r.Sequence)
	}
	return out
}

func checkMessages(t *testing.T, seed uint64, s store.Store, m *model, run core.RunID) {
	t.Helper()
	var after uint64
	for pages := 0; pages < 1000; pages++ {
		got, err := s.Messages(run, after, 5)
		want := m.messages(run, after, 5)
		if err != nil || !(reflect.DeepEqual(got, want) || (len(got) == 0 && len(want) == 0)) {
			failf(t, seed, "messages of %q after %d: %v\n got  %+v\n want %+v", run, after, err, got, want)
		}
		if len(got) == 0 {
			return
		}
		after = got[len(got)-1].Seq
	}
}

func checkTurns(t *testing.T, seed uint64, s store.Store, m *model, run core.RunID) {
	t.Helper()
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	span := m.lastTime().Sub(start)
	for _, win := range [][2]time.Duration{{0, span}, {span / 3, span / 2}, {span / 2, span}, {0, span / 10}} {
		from, to := start.Add(win[0]), start.Add(win[1])
		for _, limit := range []int{0, 3} {
			got, err := s.Turns(run, from, to, limit)
			want := m.turns(run, from, to, limit)
			if err != nil || got.Truncated != want.Truncated || !(reflect.DeepEqual(got.Turns, want.Turns) || (len(got.Turns) == 0 && len(want.Turns) == 0)) {
				failf(t, seed, "turns of %q in [%v, %v] limit %d: %v\n got  %+v\n want %+v", run, from, to, limit, err, got, want)
			}
		}
	}
}

func (m *model) lastTime() time.Time {
	if len(m.events) == 0 {
		return time.Time{}
	}
	return m.events[len(m.events)-1].Event.At
}

func checkItems(t *testing.T, seed uint64, s store.Store, m *model, agent core.AgentID) {
	t.Helper()
	before := uint32(0)
	for pages := 0; pages < 10000; pages++ {
		for _, limit := range []int{0, 4} {
			got, err := s.Items(agent, before, limit)
			want := m.items(agent, before, limit)
			if err != nil || got.HasOlder != want.HasOlder || !(reflect.DeepEqual(got.Items, want.Items) || (len(got.Items) == 0 && len(want.Items) == 0)) {
				failf(t, seed, "items of %s before %d limit %d: %v\n got  %d items hasOlder=%v\n want %d items hasOlder=%v",
					agent, before, limit, err, len(got.Items), got.HasOlder, len(want.Items), want.HasOlder)
			}
		}
		page, _ := s.Items(agent, before, 0)
		if !page.HasOlder {
			return
		}
		before = page.Items[0].Ordinal
	}
}

// farFuture is a time after every timer a test arms.
var farFuture = time.Date(2200, 1, 1, 0, 0, 0, 0, time.UTC)

// snapshotBound checks that a snapshot lists every live run and only the latest finished ones, so
// that it does not grow with the age of the installation.
func snapshotBound(t *testing.T, h *Harness) {
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	run := func(i int, status core.RunStatus) core.Event {
		return core.Event{Run: core.RunID(fmt.Sprintf("run-%03d", i)), At: at, Body: core.Run{
			ID: core.RunID(fmt.Sprintf("run-%03d", i)), Repository: "r", Task: "t", Status: status, Lead: "lead", CreatedAt: at, EndedAt: at,
		}}
	}
	total := store.MaxFinishedRuns + 30
	for i := 0; i < total; i++ {
		mustCommit(t, h.Store, store.Result{Events: []core.Event{run(i, core.RunEnded)}})
	}
	for i := total; i < total+3; i++ {
		mustCommit(t, h.Store, store.Result{Events: []core.Event{run(i, core.RunRunning)}})
	}
	// The oldest finished run changes last, so it must be listed and another dropped.
	mustCommit(t, h.Store, store.Result{Events: []core.Event{run(0, core.RunFailed)}})
	snap, err := h.Store.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Runs) != store.MaxFinishedRuns+3 {
		t.Fatalf("snapshot lists %d runs, want %d", len(snap.Runs), store.MaxFinishedRuns+3)
	}
	listed := map[core.RunID]bool{}
	for _, r := range snap.Runs {
		listed[r.ID] = true
	}
	for _, id := range []core.RunID{"run-000", "run-129", "run-130", "run-131", "run-132"} {
		if !listed[id] {
			t.Errorf("%s is missing from the snapshot", id)
		}
	}
	if listed["run-001"] {
		t.Error("run-001 changed longest ago and must be left out")
	}
}

// concurrent runs readers and an outbox claimer beside one writer. What each sees must hold whatever
// the interleaving: the log read in order has no gap, a snapshot is never behind what was read
// before it, and every effect is claimed exactly once. Readers stop when the writer is done, so the
// run has no timing in it, and a hang would be the test's timeout.
func concurrent(t *testing.T, h *Harness) {
	w := NewWorkload(11)
	var done atomic.Bool
	var wg sync.WaitGroup
	problems := make(chan string, 16)
	complain := func(format string, args ...any) {
		select {
		case problems <- fmt.Sprintf(format, args...):
		default:
		}
	}
	s := h.Store
	for r := 0; r < 3; r++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var cursor, seen uint64
			for !done.Load() {
				recs, err := s.EventsAfter("", cursor, 40)
				if err != nil {
					complain("events: %v", err)
					return
				}
				for _, rec := range recs {
					if rec.Sequence != cursor+1 {
						complain("read event %d after %d", rec.Sequence, cursor)
						return
					}
					cursor = rec.Sequence
				}
				snap, err := s.Snapshot()
				if err != nil || snap.Sequence < cursor || snap.Sequence < seen {
					complain("snapshot at %d after reading %d and seeing %d (%v)", snap.Sequence, cursor, seen, err)
					return
				}
				seen = snap.Sequence
				for _, a := range snap.Agents {
					if _, err := s.Items(a.ID, 0, 3); err != nil {
						complain("items: %v", err)
						return
					}
				}
			}
		}()
	}
	claimed := map[core.EffectID]int{}
	var mu sync.Mutex
	wg.Add(1)
	go func() {
		defer wg.Done()
		// A store that hands out an effect twice would never run dry, so the loop is bounded by
		// what could ever be claimed: far more than one writer commits.
		for rounds := 0; rounds < 100000; rounds++ {
			finished := done.Load()
			got, err := s.Claim(farFuture, 10)
			if err != nil {
				complain("claim: %v", err)
				return
			}
			var ids []store.OutboxID
			mu.Lock()
			for _, c := range got {
				claimed[c.Effect.ID()]++
				ids = append(ids, c.ID)
			}
			mu.Unlock()
			if err := s.Complete(ids...); err != nil {
				complain("complete: %v", err)
				return
			}
			if finished && len(got) == 0 {
				return
			}
		}
	}()
	total := map[core.EffectID]int{}
	for range 150 {
		r := w.Next()
		if _, err := s.Commit(r); err != nil {
			t.Fatalf("commit: %v", err)
		}
		for _, e := range r.Effects {
			total[e.ID()]++
		}
	}
	done.Store(true)
	wg.Wait()
	close(problems)
	for p := range problems {
		t.Error(p)
	}
	mu.Lock()
	defer mu.Unlock()
	if !reflect.DeepEqual(claimed, total) {
		t.Errorf("effects claimed %d distinct, committed %d distinct: each must be claimed exactly as often as it was committed", len(claimed), len(total))
	}
}
