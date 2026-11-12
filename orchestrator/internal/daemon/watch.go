package daemon

import (
	"errors"
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// ErrAheadOfLog says a client asked to continue after a sequence the log has not reached, so it
// holds state from a different history. It starts again from a snapshot.
var ErrAheadOfLog = errors.New("daemon: that sequence is ahead of the log; start again from a snapshot")

// watch is the shared machinery of a stream: a snapshot, then the public part of every event after it, and a fresh
// snapshot for a client that fell behind its bounds. It never blocks: Poll returns what is ready,
// and Ready says when to call it again. R is the response type of the stream.
type watch[R any] struct {
	e        *Engine
	sub      *hub.Subscription
	sent     uint64
	maxBytes int
	// run limits the events the stream reads from the log; empty reads every run.
	run core.RunID
	// snapshot builds the stream's snapshot from the instant of a Cut.
	snapshot func(state core.State, last uint64) (R, error)
	change   func(*pb.RunChange) (R, bool)
	delta    func(*pb.Delta) (R, bool)
	latest   func(*pb.SearchState) (R, bool)
	// stamp writes the committed sequence a response is current to. Most watch responses carry
	// their cursor inside their body; WatchAgentResponse carries it on every arm, so the generic
	// watch stamps each path instead of asking every constructor to remember it.
	stamp func(R, uint64)
	// setup is told that a run's setup observation moved. It sends nothing itself: a stream whose
	// frames are whole views notes the run, and settle sends the view.
	setup func(hub.SetupNote)
	// current is what a client is owed after a snapshot or a replay, from the state at that instant, and
	// says whether it was too much for one burst, so that only a prefix of it is returned.
	current func(core.State) (frames []R, tooMany bool)
	// settle, when set, is called at the end of every Poll with the sequence the stream is current to,
	// and returns what the events of the batch and the time since have added up to. A stream whose
	// frames are whole views uses it to send one view for a batch of events, not one for each.
	settle func(sent uint64) []R
}

func (w *watch[R]) stamped(response R, sequence uint64) R {
	if w.stamp != nil {
		w.stamp(response, sequence)
	}
	return response
}

func (w *watch[R]) stampAll(responses []R, sequence uint64) {
	if w.stamp == nil {
		return
	}
	for _, response := range responses {
		w.stamp(response, sequence)
	}
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
		return w.checked(first)
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
			msg, err := wire.ChangeMessage(rec.Sequence, rec.Event)
			if err != nil {
				w.sub.Close()
				return nil, err
			}
			if r, ok := w.change(msg); ok {
				first = append(first, w.stamped(r, rec.Sequence))
			}
		}
	}
	// A run-limited read skips the sequences of other runs, so the cursor may stop short of target.
	w.sent = max(w.sent, target)
	if w.current != nil {
		tooMany := false
		if err := w.e.Cut(func(state core.State, _ uint64) error {
			var frames []R
			frames, tooMany = w.current(state)
			w.stampAll(frames, w.sent)
			first = append(first, frames...)
			return nil
		}); err != nil {
			return nil, err
		}
		if tooMany {
			// A client that resumed holds views that may be stale, and a burst that cannot refresh them all
			// would leave some of them so: a snapshot says what it left out.
			return w.resync()
		}
	}
	return w.recoverPastOversized(first)
}

func (w *watch[R]) snapshotLocked(state core.State, last uint64, out *[]R) error {
	snap, err := w.snapshot(state, last)
	if err != nil {
		return err
	}
	w.sent = last
	*out = append(*out, w.stamped(snap, last))
	if w.current != nil {
		// Of a snapshot's follow-ups, the runs that do not fit the burst are sent what they send when
		// they next change.
		frames, _ := w.current(state)
		w.stampAll(frames, last)
		*out = append(*out, frames...)
	}
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
	if err != nil {
		return nil, err
	}
	return w.checked(out)
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
			seq := m.Stored.Change.GetSequence()
			if seq <= w.sent {
				continue
			}
			w.sent = seq
			if r, ok := w.change(m.Stored.Change); ok {
				out = append(out, w.stamped(r, seq))
			}
		case m.Search != nil && w.latest != nil:
			if r, ok := w.latest(m.Search); ok {
				out = append(out, w.stamped(r, w.sent))
			}
		case m.Setup != nil && w.setup != nil:
			w.setup(*m.Setup)
		case m.Delta != nil:
			if r, ok := w.delta(m.Delta); ok {
				out = append(out, w.stamped(r, w.sent))
			}
		}
	}
	if w.settle != nil {
		settled := w.settle(w.sent)
		w.stampAll(settled, w.sent)
		out = append(out, settled...)
	}
	return w.recoverPastOversized(out)
}

// Ready is signalled when Poll may have something.
func (w *watch[R]) Ready() <-chan struct{} { return w.sub.Ready() }

// Close ends the stream.
func (w *watch[R]) Close() { w.sub.Close() }

// RunsWatch is a WatchRuns stream.
type RunsWatch = watch[*pb.WatchRunsResponse]

// AgentWatch is a WatchAgent stream.
type AgentWatch = watch[*pb.WatchAgentResponse]

// checked refuses an oversized frame before emitting it on the transport.
func (w *watch[R]) checked(out []R) ([]R, error) {
	if w.maxBytes > 0 {
		for _, frame := range out {
			if proto.Size(any(frame).(proto.Message)) > w.maxBytes {
				return nil, status.Error(codes.ResourceExhausted, "orchestrator response exceeds frame byte budget")
			}
		}
	}
	return out, nil
}

// recoverPastOversized checks a batch of changes. A legacy event can be larger than today's wire
// contract; the stream has already applied its sequence, so replace the unsendable batch with a
// bounded snapshot at the current cut. A snapshot is checked without recovery: if it is oversized,
// there is no later cursor to advance to and RESOURCE_EXHAUSTED is the truthful terminal answer.
func (w *watch[R]) recoverPastOversized(out []R) ([]R, error) {
	checked, err := w.checked(out)
	if err == nil {
		return checked, nil
	}
	return w.resync()
}
