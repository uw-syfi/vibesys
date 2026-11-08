package daemon

import (
	"errors"
	"fmt"
	"log/slog"
	"sync"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// ErrCommandConflict says a command id was already used for another command.
var ErrCommandConflict = errors.New("daemon: this command id was used for another command")

// EngineDeps is what an Engine is made of.
type EngineDeps struct {
	Search *search.Store
	Store  store.Store
	Clock  Clock
	Nonces Nonces
	Hub    *hub.Hub
	Log    *slog.Logger
	// Models says the host's model policy. Nil means the host has none.
	Models ModelPolicySource
	// OnCommit is called after every commit, with the engine's lock held, so it must not wait. The
	// daemon's loop uses it to wake up.
	OnCommit func()
}

// Engine is the serial loop: every input passes through Apply one at a time, is decided, committed,
// and only then shown to watchers.
type Engine struct {
	EngineDeps
	mu       sync.Mutex
	searches map[core.RunID]*pb.SearchState
	reports  reportCache
	state    core.State
	last     uint64
	// touched is the sequence of the last event committed for each run, which the setup observation
	// compares with the one it saw last. It is written in apply, under the lock, and does no I/O.
	touched map[core.RunID]uint64
}

// OpenEngine recovers the state from the store. It is the whole of starting up: a daemon that
// crashed and one that was stopped start the same way.
func OpenEngine(d EngineDeps) (*Engine, error) {
	state, err := d.Store.Recover()
	if err != nil {
		return nil, fmt.Errorf("recover the state: %w", err)
	}
	if d.Log == nil {
		d.Log = slog.New(slog.DiscardHandler)
	}
	if state, err = nameSummaryReports(d, state); err != nil {
		return nil, err
	}
	snap, err := d.Store.Snapshot()
	if err != nil {
		return nil, fmt.Errorf("read the last sequence: %w", err)
	}
	e := &Engine{EngineDeps: d, state: state, last: snap.Sequence, searches: map[core.RunID]*pb.SearchState{}, reports: reportCache{}, touched: map[core.RunID]uint64{}}
	return e, nil
}

// ModelPolicy is the host's model policy now: the zero value, which restricts nothing, when the host
// has no source of one.
func (e *Engine) ModelPolicy() core.ModelPolicy {
	if e.Models == nil {
		return core.ModelPolicy{}
	}
	return e.Models.ModelPolicy()
}

// launchesAgents says the command starts an agent, so that is the only input the policy is read for.
func launchesAgents(c core.Command) bool {
	switch c.Body.(type) {
	case core.StartRun, core.SpawnAgent:
		return true
	}
	return false
}

// Applied says what one input did.
type Applied struct {
	// Events are the events committed, with the sequence of the first. Empty when the input changed
	// nothing.
	Events []core.Event
	First  uint64
}

// Apply decides one input and commits the result. It stamps the input with the time and a nonce,
// which are the only places the shell supplies what the core may not find out. If the commit fails
// the state is as it was, so the caller may try the same fact again.
func (e *Engine) Apply(body core.InputBody) (Applied, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.apply(body)
}

func (e *Engine) apply(body core.InputBody) (Applied, error) {
	in := core.Input{At: e.Clock.Now(), Nonce: e.Nonces.Next(), Body: body}
	if c, ok := body.(core.Command); ok && launchesAgents(c) {
		in.Models = e.ModelPolicy()
	}
	next, events, effects := core.Decide(e.state, in)
	if len(events) == 0 && len(effects) == 0 {
		// Decide records no reason for an input that changes nothing. Most are repeats a feed or a
		// retry is expected to send, so this is the only trace of them.
		e.Log.Debug("input changed nothing", "input", fmt.Sprintf("%T", body), "at", in.At)
		return Applied{}, nil
	}
	committed, err := e.Store.Commit(store.Result{Events: events, Effects: effects})
	if err != nil {
		return Applied{}, fmt.Errorf("commit %T: %w", body, err)
	}
	e.state = next
	if committed.Last > 0 {
		e.last = committed.Last
	}
	for i, ev := range events {
		e.publish(committed.First+uint64(i), ev)
	}
	if e.OnCommit != nil {
		e.OnCommit()
	}
	return Applied{Events: events, First: committed.First}, nil
}

func (e *Engine) publish(seq uint64, ev core.Event) {
	msg, err := wire.ChangeMessage(seq, ev)
	if err != nil {
		// The store accepted the event, so it encodes; a failure here is a bug in this build.
		e.Log.Error("cannot show a committed event to watchers", "sequence", seq, "err", err)
		return
	}
	if run := core.RunID(msg.GetRunId()); run != "" {
		e.touched[run] = seq
	}
	st := hub.Stored{Change: msg}
	switch b := ev.Body.(type) {
	case core.Item:
		st.Agent, st.Item, st.ItemFinal = b.Agent, b.ID, b.Status != core.ItemRunning
	case core.Turn:
		st.Agent = b.Agent
	case core.Agent:
		st.Agent = b.ID
	case core.Message:
		st.Agent = b.To
	}
	e.Hub.PublishStored(st)
}

// Answer is the reply to a command.
type Answer struct {
	Receipt core.Receipt
	// Sequence is a sequence at which the command's effects are visible: the receipt's own event, or
	// for a repeat, the last one committed.
	Sequence uint64
}

// Command answers a command from a caller. A repeat of a command gets its stored receipt; the same
// id on another command is ErrCommandConflict; anything else goes to Decide.
func (e *Engine) Command(c core.Command) (Answer, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	switch receipt, recall := e.state.Recall(c.ID, c.Digest); recall {
	case core.RecallRepeat:
		return Answer{Receipt: receipt, Sequence: e.last}, nil
	case core.RecallConflict:
		return Answer{}, fmt.Errorf("%w: %s", ErrCommandConflict, c.ID)
	}
	applied, err := e.apply(c)
	if err != nil {
		return Answer{}, err
	}
	for i, ev := range applied.Events {
		if r, ok := ev.Body.(core.Receipt); ok && r.Command == c.ID {
			return Answer{Receipt: r, Sequence: applied.First + uint64(i)}, nil
		}
	}
	return Answer{}, fmt.Errorf("command %s was decided with no receipt", c.ID)
}

// State returns the state now. It is a value that later commits leave as it is.
func (e *Engine) State() core.State {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.state
}

// Last is the sequence of the last committed event.
func (e *Engine) Last() uint64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.last
}

// Cut runs fn at one instant of the log: nothing commits and nothing is published while it runs.
// A reader that subscribes to the hub and reads a snapshot inside Cut misses no event and sees
// none twice.
func (e *Engine) Cut(fn func(state core.State, last uint64) error) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return fn(e.state, e.last)
}

// Delta shows live progress to the clients watching its agent. It goes through the engine's lock,
// so a delta is ordered against the commits around it.
func (e *Engine) Delta(d *pb.Delta) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.Hub.PublishDelta(d)
}
