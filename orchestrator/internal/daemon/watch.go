package daemon

import (
	"errors"
	"fmt"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// ErrAheadOfLog says a client asked to continue after a sequence the log has not reached, so it
// holds state from a different history. It starts again from a snapshot.
var ErrAheadOfLog = errors.New("daemon: that sequence is ahead of the log; start again from a snapshot")

// watch is the shared machinery of a stream: a snapshot, then every event after it, and a fresh
// snapshot for a client that fell behind its bounds. It never blocks: Poll returns what is ready,
// and Ready says when to call it again. R is the response type of the stream.
type watch[R any] struct {
	e    *Engine
	sub  *hub.Subscription
	sent uint64
	// run limits the events the stream reads from the log; empty reads every run.
	run core.RunID
	// snapshot builds the stream's snapshot from the instant of a Cut.
	snapshot func(state core.State, last uint64) (R, error)
	event    func(*pb.Event) (R, bool)
	delta    func(*pb.Delta) (R, bool)
	latest   func(*pb.SearchState) (R, bool)
	current  func(core.State) []R
}

// open starts the stream. With after zero the first response is a snapshot; otherwise the stream
// replays the log after that sequence, or starts with a snapshot when the log no longer holds it.
func (w *watch[R]) open(interest hub.Interest, after uint64) ([]R, error) {
	var first []R
	err := w.e.Cut(func(state core.State, last uint64) error {
		w.sub = w.e.Hub.Subscribe(interest)
		switch {
		case after == 0:
			return w.snapshotLocked(state, last, &first)
		case after > last:
			return fmt.Errorf("%w (asked after %d, the log is at %d)", ErrAheadOfLog, after, last)
		}
		w.sent = after
		return nil
	})
	if err != nil {
		if w.sub != nil {
			w.sub.Close()
		}
		return nil, err
	}
	if after == 0 {
		return first, nil
	}
	target := w.e.Last()
	for w.sent < target {
		records, err := w.e.Store.EventsAfter(w.run, w.sent, store.MaxPageEvents)
		if errors.Is(err, store.ErrCompacted) {
			return w.resync()
		}
		if err != nil {
			w.sub.Close()
			return nil, fmt.Errorf("read events after %d: %w", w.sent, err)
		}
		if len(records) == 0 {
			break
		}
		for _, rec := range records {
			w.sent = rec.Sequence
			msg, err := wire.EventMessage(rec.Sequence, rec.Event)
			if err != nil {
				w.sub.Close()
				return nil, err
			}
			if r, ok := w.event(msg); ok {
				first = append(first, r)
			}
		}
	}
	// A run-limited read skips the sequences of other runs, so the cursor may stop short of target.
	w.sent = max(w.sent, target)
	if w.current != nil {
		if err := w.e.Cut(func(state core.State, _ uint64) error { first = append(first, w.current(state)...); return nil }); err != nil {
			return nil, err
		}
	}
	return first, nil
}

func (w *watch[R]) snapshotLocked(state core.State, last uint64, out *[]R) error {
	snap, err := w.snapshot(state, last)
	if err != nil {
		return err
	}
	w.sent = last
	*out = append(*out, snap)
	return nil
}

// resync takes a fresh snapshot, and ends a lag.
func (w *watch[R]) resync() ([]R, error) {
	var out []R
	err := w.e.Cut(func(state core.State, last uint64) error {
		if err := w.snapshotLocked(state, last, &out); err != nil {
			return err
		}
		w.sub.Resume()
		return nil
	})
	return out, err
}

// Poll returns what is ready now, in order, and nothing if nothing is.
func (w *watch[R]) Poll() ([]R, error) {
	msgs, lagged := w.sub.Take()
	if lagged {
		return w.resync()
	}
	var out []R
	for _, m := range msgs {
		switch {
		case m.Stored != nil:
			seq := m.Stored.Event.GetSequence()
			if seq <= w.sent {
				continue
			}
			w.sent = seq
			if r, ok := w.event(m.Stored.Event); ok {
				out = append(out, r)
			}
		case m.Search != nil && w.latest != nil:
			if r, ok := w.latest(m.Search); ok {
				out = append(out, r)
			}
		case m.Delta != nil:
			if r, ok := w.delta(m.Delta); ok {
				out = append(out, r)
			}
		}
	}
	return out, nil
}

// Ready is signalled when Poll may have something.
func (w *watch[R]) Ready() <-chan struct{} { return w.sub.Ready() }

// Close ends the stream.
func (w *watch[R]) Close() { w.sub.Close() }

// RunsWatch is a WatchRuns stream.
type RunsWatch = watch[*pb.WatchRunsResponse]

// AgentWatch is a WatchAgent stream.
type AgentWatch = watch[*pb.WatchAgentResponse]
