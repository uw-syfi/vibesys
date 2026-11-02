package store

import (
	"cmp"
	"errors"
	"fmt"
	"slices"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// Memory is a Store that keeps everything in memory. It is the Fake for tests of the daemon, and
// it passes the same contract as SQLite: it encodes every event and effect, refuses what the file's
// unique keys refuse, and serves reads in the same order with the same caps.
//
// Reopen gives a new Store over the committed data, as a restart would: claims are forgotten and
// the old Store is closed. A Memory is not safe against a crash in the middle of Commit, because
// it has no middle: Commit either returns an error before changing anything or finishes.
type Memory struct {
	shared *memoryData
	closed bool
}

// memoryData is what outlives a Store: the "disk".
type memoryData struct {
	mu sync.Mutex
	// events is the retained log, in sequence order.
	events []logged
	last   uint64
	tables *tables
	outbox []outboxRow
	nextID OutboxID
	// checkpoint is the state at sequence checkpointSeq. Events above it are replayed over it.
	checkpoint    core.State
	checkpointSeq uint64
	// pruned is, per run, the last sequence Compact dropped. Empty run is for commands refused
	// before a run.
	pruned    map[core.RunID]uint64
	prunedMax uint64
	// owner is the Memory that may use the data, so that a Store replaced by Reopen stops working.
	owner *Memory
}

type logged struct {
	seq  uint64
	run  core.RunID
	at   time.Time
	body []byte
}

type outboxRow struct {
	id      OutboxID
	effect  []byte
	after   int64
	timed   bool
	claimed bool
}

// NewMemory returns an empty Store.
func NewMemory() *Memory {
	d := &memoryData{tables: newTables(), pruned: map[core.RunID]uint64{}}
	m := &Memory{shared: d}
	d.owner = m
	return m
}

// Reopen returns a Store over the same committed data, as a restart does. The receiver is closed.
func (m *Memory) Reopen() *Memory {
	d := m.shared
	d.mu.Lock()
	defer d.mu.Unlock()
	m.closed = true
	next := &Memory{shared: d}
	d.owner = next
	for i := range d.outbox {
		d.outbox[i].claimed = false
	}
	return next
}

var errClosed = errors.New("store: closed")

// lock takes the data's lock and checks that this Memory is the one in use.
func (m *Memory) lock() (unlock func(), err error) {
	d := m.shared
	d.mu.Lock()
	if m.closed || d.owner != m {
		d.mu.Unlock()
		return nil, errClosed
	}
	return d.mu.Unlock, nil
}

// Close implements Store.
func (m *Memory) Close() error {
	m.shared.mu.Lock()
	defer m.shared.mu.Unlock()
	m.closed = true
	return nil
}

// Commit implements Store.
func (m *Memory) Commit(r Result) (Committed, error) {
	unlock, err := m.lock()
	if err != nil {
		return Committed{}, err
	}
	defer unlock()
	d := m.shared
	if len(r.Events) == 0 && len(r.Effects) == 0 {
		return Committed{}, nil
	}
	events, effects, err := encodeResult(d.last+1, r)
	if err != nil {
		return Committed{}, err
	}
	// Decode what was encoded, so a table holds what a reader will get, and so that an event the
	// wire layer would refuse on the way back is refused here rather than at the next restart.
	decoded := make([]core.Event, len(events))
	c := newClash(d.tables)
	for i, b := range events {
		if _, decoded[i], err = wire.DecodeEvent(b); err != nil {
			return Committed{}, fmt.Errorf("store: event %d does not read back: %w", i+1, err)
		}
		if err := c.check(decoded[i]); err != nil {
			return Committed{}, err
		}
	}
	for i, b := range effects {
		if _, err := wire.DecodeEffect(b); err != nil {
			return Committed{}, fmt.Errorf("store: effect %d does not read back: %w", i+1, err)
		}
	}
	first := d.last + 1
	for i, b := range events {
		seq := first + uint64(i)
		if err := d.tables.apply(seq, decoded[i], b); err != nil {
			return Committed{}, err
		}
		d.events = append(d.events, logged{seq: seq, run: r.Events[i].Run, at: r.Events[i].At, body: b})
	}
	d.last += uint64(len(events))
	for i, b := range effects {
		after, timed := runAfter(r.Effects[i])
		d.nextID++
		d.outbox = append(d.outbox, outboxRow{id: d.nextID, effect: b, after: after, timed: timed})
	}
	return Committed{First: first, Last: d.last}, nil
}

// Recover implements Store.
func (m *Memory) Recover() (core.State, error) {
	unlock, err := m.lock()
	if err != nil {
		return core.State{}, err
	}
	defer unlock()
	d := m.shared
	var tail []core.Event
	for _, l := range d.events {
		if l.seq <= d.checkpointSeq {
			continue
		}
		_, e, err := wire.DecodeEvent(l.body)
		if err != nil {
			return core.State{}, corruptf("event %d: %v", l.seq, err)
		}
		tail = append(tail, e)
	}
	return core.Replay(d.checkpoint, tail), nil
}

// Claim implements Store.
func (m *Memory) Claim(now time.Time, limit int) ([]Claimed, error) {
	unlock, err := m.lock()
	if err != nil {
		return nil, err
	}
	defer unlock()
	var out []Claimed
	for i := range m.shared.outbox {
		row := &m.shared.outbox[i]
		if len(out) >= limit {
			break
		}
		if row.claimed || (row.timed && row.after > now.UnixNano()) {
			continue
		}
		e, err := wire.DecodeEffect(row.effect)
		if err != nil {
			return nil, corruptf("outbox row %d: %v", row.id, err)
		}
		row.claimed = true
		out = append(out, Claimed{ID: row.id, Effect: e})
	}
	return out, nil
}

// Complete implements Store.
func (m *Memory) Complete(ids ...OutboxID) error {
	unlock, err := m.lock()
	if err != nil {
		return err
	}
	defer unlock()
	m.shared.outbox = slices.DeleteFunc(m.shared.outbox, func(r outboxRow) bool {
		return r.claimed && slices.Contains(ids, r.id)
	})
	return nil
}

// Release implements Store.
func (m *Memory) Release(ids ...OutboxID) error {
	unlock, err := m.lock()
	if err != nil {
		return err
	}
	defer unlock()
	for i := range m.shared.outbox {
		if slices.Contains(ids, m.shared.outbox[i].id) {
			m.shared.outbox[i].claimed = false
		}
	}
	return nil
}

// NextDue implements Store.
func (m *Memory) NextDue() (time.Time, bool, error) {
	unlock, err := m.lock()
	if err != nil {
		return time.Time{}, false, err
	}
	defer unlock()
	var best int64
	found := false
	for _, r := range m.shared.outbox {
		if r.timed && !r.claimed && (!found || r.after < best) {
			best, found = r.after, true
		}
	}
	return time.Unix(0, best).UTC(), found, nil
}

// Compact implements Store.
func (m *Memory) Compact(p Retention, now time.Time) (Compaction, error) {
	unlock, err := m.lock()
	if err != nil {
		return Compaction{}, err
	}
	defer unlock()
	d := m.shared
	var out Compaction
	if d.last-d.checkpointSeq > 0 && d.last-d.checkpointSeq >= p.CheckpointAfter {
		var tail []core.Event
		for _, l := range d.events {
			if l.seq > d.checkpointSeq {
				_, e, err := wire.DecodeEvent(l.body)
				if err != nil {
					return Compaction{}, corruptf("event %d: %v", l.seq, err)
				}
				tail = append(tail, e)
			}
		}
		d.checkpoint, d.checkpointSeq = core.Replay(d.checkpoint, tail), d.last
		out.Checkpointed = true
	}
	out.Checkpoint = d.checkpointSeq
	keep := d.events[:0:0]
	for _, l := range d.events {
		if l.seq <= d.checkpointSeq && m.droppable(l, p, now) {
			d.pruned[l.run] = max(d.pruned[l.run], l.seq)
			d.prunedMax = max(d.prunedMax, l.seq)
			out.EventsDropped++
			continue
		}
		keep = append(keep, l)
	}
	d.events = keep
	return out, nil
}

// droppable says an event may leave the log: it belongs to a finished run that ended long enough
// ago, or to a command refused before any run, and is that old.
func (m *Memory) droppable(l logged, p Retention, now time.Time) bool {
	if l.run == "" {
		return !l.at.Add(p.KeepEvents).After(now)
	}
	r, ok := m.shared.tables.runs[l.run]
	if !ok {
		return false
	}
	run := r.event.Body.(core.Run)
	return run.Status.Final() && !run.EndedAt.Add(p.KeepEvents).After(now)
}

// Verify implements Store.
func (m *Memory) Verify() error {
	unlock, err := m.lock()
	if err != nil {
		return err
	}
	defer unlock()
	d := m.shared
	want := newTables()
	for _, l := range d.events {
		_, e, err := wire.DecodeEvent(l.body)
		if err != nil {
			return corruptf("event %d: %v", l.seq, err)
		}
		if err := want.apply(l.seq, e, l.body); err != nil {
			return err
		}
	}
	// A memory store's tables are the projection, so what remains is that dropping events kept the
	// tail whole: sequences above the checkpoint have no gap.
	next := d.checkpointSeq + 1
	for _, l := range d.events {
		if l.seq <= d.checkpointSeq {
			continue
		}
		if l.seq != next {
			return corruptf("sequence %d follows %d", l.seq, next-1)
		}
		next++
	}
	if next-1 != max(d.last, d.checkpointSeq) {
		return corruptf("log ends at %d, store says %d", next-1, d.last)
	}
	return nil
}

// Snapshot implements Reader.
func (m *Memory) Snapshot() (Snapshot, error) {
	unlock, err := m.lock()
	if err != nil {
		return Snapshot{}, err
	}
	defer unlock()
	t := m.shared.tables
	out := Snapshot{Sequence: m.shared.last}
	for _, r := range t.runs {
		out.Runs = append(out.Runs, r.event.Body.(core.Run))
	}
	for _, r := range t.agents {
		out.Agents = append(out.Agents, r.event.Body.(core.Agent))
	}
	slices.SortFunc(out.Runs, func(a, b core.Run) int { return cmp.Compare(a.ID, b.ID) })
	slices.SortFunc(out.Agents, func(a, b core.Agent) int { return cmp.Compare(a.ID, b.ID) })
	return out, nil
}

// EventsAfter implements Reader.
func (m *Memory) EventsAfter(run core.RunID, after uint64, limit int) ([]EventRecord, error) {
	unlock, err := m.lock()
	if err != nil {
		return nil, err
	}
	defer unlock()
	d := m.shared
	if (run == "" && after < d.prunedMax) || (run != "" && after < d.pruned[run]) {
		return nil, ErrCompacted
	}
	limit = pageLimit(limit, MaxPageEvents)
	var out []EventRecord
	for _, l := range d.events {
		if l.seq <= after || (run != "" && l.run != run) {
			continue
		}
		if len(out) == limit {
			break
		}
		seq, e, err := wire.DecodeEvent(l.body)
		if err != nil {
			return nil, corruptf("event %d: %v", l.seq, err)
		}
		out = append(out, EventRecord{Sequence: seq, Event: e})
	}
	return out, nil
}

// Items implements Reader.
func (m *Memory) Items(agent core.AgentID, before uint32, limit int) (ItemPage, error) {
	unlock, err := m.lock()
	if err != nil {
		return ItemPage{}, err
	}
	defer unlock()
	var all []row
	for _, r := range m.shared.tables.items {
		if i := r.event.Body.(core.Item); i.Agent == agent && (before == 0 || i.Ordinal < before) {
			all = append(all, r)
		}
	}
	slices.SortFunc(all, func(a, b row) int {
		return cmp.Compare(b.event.Body.(core.Item).Ordinal, a.event.Body.(core.Item).Ordinal)
	})
	limit = pageLimit(limit, MaxPageItems)
	var page ItemPage
	size := 0
	for n, r := range all {
		if n == limit || (n > 0 && size+len(r.body) > MaxPageBytes) {
			page.HasOlder = true
			break
		}
		size += len(r.body)
		page.Items = append(page.Items, r.event.Body.(core.Item))
	}
	slices.Reverse(page.Items)
	return page, nil
}

// Messages implements Reader.
func (m *Memory) Messages(run core.RunID, after uint64, limit int) ([]MessageRecord, error) {
	unlock, err := m.lock()
	if err != nil {
		return nil, err
	}
	defer unlock()
	var all []MessageRecord
	for _, r := range m.shared.tables.messages {
		if msg := r.event.Body.(core.Message); msg.Run == run && r.first > after {
			all = append(all, MessageRecord{Seq: r.first, Message: msg})
		}
	}
	slices.SortFunc(all, func(a, b MessageRecord) int { return cmp.Compare(a.Seq, b.Seq) })
	return all[:min(len(all), pageLimit(limit, MaxPageMessages))], nil
}

// Turns implements Reader.
func (m *Memory) Turns(run core.RunID, from, to time.Time, limit int) (TurnWindow, error) {
	unlock, err := m.lock()
	if err != nil {
		return TurnWindow{}, err
	}
	defer unlock()
	var all []core.Turn
	for _, r := range m.shared.tables.turns {
		t := r.event.Body.(core.Turn)
		if r.event.Run != run {
			continue
		}
		running := t.EndedAt.IsZero()
		if t.StartedAt.Before(to) && (running || t.EndedAt.After(from)) {
			all = append(all, t)
		}
	}
	slices.SortFunc(all, func(a, b core.Turn) int {
		return cmp.Or(a.StartedAt.Compare(b.StartedAt), cmp.Compare(a.ID, b.ID))
	})
	limit = pageLimit(limit, MaxWindowTurns)
	return TurnWindow{Turns: all[:min(len(all), limit)], Truncated: len(all) > limit}, nil
}
