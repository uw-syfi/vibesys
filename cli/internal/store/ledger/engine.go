package ledger

import (
	"context"
	"errors"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// Backend is where an Engine's changes go. It is the only part of a store that differs between
// implementations, and it is never asked to decide anything.
type Backend interface {
	// Commit makes a change durable, all or nothing. After an error the change may or may not be
	// durable, and the Engine finds out by calling Recover before it serves another request.
	Commit(Change) error

	// Recover reads the durable state. It runs when the Engine starts, and again after a Commit
	// failed, so the path that follows a failure is the path that follows a crash.
	Recover() (Snapshot, error)

	// Events returns the events of a run at positions first to last, both included, in order.
	// They exist: the Ledger counted them.
	Events(run core.RunID, first, last uint64) ([]core.Event, error)

	// Log returns bytes from to to, with to excluded, of a job's log. They exist.
	Log(job core.JobID, from, to uint64) ([]byte, error)
}

// Engine is a store.Store over a Ledger and a Backend. It is safe for concurrent use: one request
// runs at a time, so a request sees the whole of every request before it or none of it.
type Engine struct {
	mu      sync.Mutex
	backend Backend
	rules   store.Rules
	ledger  *Ledger
	// stale is set when a Commit failed, so the Ledger may differ from the durable state.
	stale bool
}

var _ store.Store = (*Engine)(nil)

// NewEngine recovers the backend's durable state and serves it, applying the rules.
func NewEngine(backend Backend, rules store.Rules) (*Engine, error) {
	e := &Engine{backend: backend, rules: rules, stale: true}
	if err := e.recover(); err != nil {
		return nil, err
	}
	return e, nil
}

func (e *Engine) recover() error {
	if !e.stale {
		return nil
	}
	snap, err := e.backend.Recover()
	if err != nil {
		return failure(err)
	}
	ledger, err := Restore(e.rules, snap)
	if err != nil {
		return err
	}
	e.ledger, e.stale = ledger, false
	return nil
}

// failure keeps a backend's own typed failure and makes every other error an internal one.
func failure(err error) error {
	var f *core.Failure
	if errors.As(err, &f) {
		return f
	}
	return &core.Failure{Kind: core.FailureInternal, Message: err.Error()}
}

// read runs a request that changes nothing.
func read[T any](e *Engine, ctx context.Context, request func(*Ledger) (T, error)) (T, error) {
	var none T
	if err := ctx.Err(); err != nil {
		return none, err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return none, err
	}
	if err := e.recover(); err != nil {
		return none, err
	}
	return request(e.ledger)
}

// write runs a request that decides a change: the change is made durable, and only then does it
// show in a read.
func write[T any](e *Engine, ctx context.Context, decide func(*Ledger) (Change, T, error)) (T, error) {
	var none T
	if err := ctx.Err(); err != nil {
		return none, err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return none, err
	}
	if err := e.recover(); err != nil {
		return none, err
	}
	change, answer, err := decide(e.ledger)
	if err != nil {
		return none, err
	}
	if change == nil {
		return answer, nil
	}
	if err := e.backend.Commit(change); err != nil {
		e.stale = true
		return none, failure(err)
	}
	e.ledger.Apply(change)
	return answer, nil
}

// Submit implements store.JobStore.
func (e *Engine) Submit(ctx context.Context, submission core.Submission) (core.SubmitOutcome, error) {
	return write(e, ctx, func(l *Ledger) (Change, core.SubmitOutcome, error) { return l.Submit(submission) })
}

// Job implements store.JobStore.
func (e *Engine) Job(ctx context.Context, id core.JobID) (core.Job, error) {
	return read(e, ctx, func(l *Ledger) (core.Job, error) { return l.Job(id) })
}

// Jobs implements store.JobStore.
func (e *Engine) Jobs(ctx context.Context, filter core.JobFilter) ([]core.Job, error) {
	return read(e, ctx, func(l *Ledger) ([]core.Job, error) { return l.Jobs(filter), nil })
}

// Transition implements store.JobStore.
func (e *Engine) Transition(ctx context.Context, transition core.Transition) (core.Job, error) {
	return write(e, ctx, func(l *Ledger) (Change, core.Job, error) { return l.Transition(transition) })
}

// AddNote implements store.JobStore.
func (e *Engine) AddNote(ctx context.Context, id core.JobID, note core.Note) (core.Job, error) {
	return write(e, ctx, func(l *Ledger) (Change, core.Job, error) { return l.AddNote(id, note) })
}

// Results implements store.ResultStore.
func (e *Engine) Results(ctx context.Context, filter core.ResultFilter) ([]core.Result, error) {
	return read(e, ctx, func(l *Ledger) ([]core.Result, error) { return l.Results(filter), nil })
}

// RecordPromotion implements store.ResultStore.
func (e *Engine) RecordPromotion(ctx context.Context, promotion core.Promotion) error {
	_, err := write(e, ctx, func(l *Ledger) (Change, struct{}, error) {
		change, err := l.RecordPromotion(promotion)
		return change, struct{}{}, err
	})
	return err
}

// Promotions implements store.ResultStore.
func (e *Engine) Promotions(ctx context.Context) ([]core.Promotion, error) {
	return read(e, ctx, func(l *Ledger) ([]core.Promotion, error) { return l.Promotions(), nil })
}

// CreatePlan implements store.PlanStore.
func (e *Engine) CreatePlan(ctx context.Context, plan core.NewPlan) (core.Plan, error) {
	return write(e, ctx, func(l *Ledger) (Change, core.Plan, error) { return l.CreatePlan(plan) })
}

// Plan implements store.PlanStore.
func (e *Engine) Plan(ctx context.Context, number core.PlanNumber) (core.Plan, error) {
	return read(e, ctx, func(l *Ledger) (core.Plan, error) { return l.Plan(number) })
}

// Plans implements store.PlanStore.
func (e *Engine) Plans(ctx context.Context, filter core.PlanFilter) ([]core.Plan, error) {
	return read(e, ctx, func(l *Ledger) ([]core.Plan, error) { return l.Plans(filter), nil })
}

// EditPlan implements store.PlanStore.
func (e *Engine) EditPlan(ctx context.Context, number core.PlanNumber, edit core.PlanEdit) (core.Plan, error) {
	return write(e, ctx, func(l *Ledger) (Change, core.Plan, error) { return l.EditPlan(number, edit) })
}

// AppendEvent implements store.EventStore.
func (e *Engine) AppendEvent(ctx context.Context, draft core.EventDraft) (core.Event, error) {
	return write(e, ctx, func(l *Ledger) (Change, core.Event, error) { return l.AppendEvent(draft) })
}

// Events implements store.EventStore.
func (e *Engine) Events(ctx context.Context, run core.RunID, after uint64, limit int) ([]core.Event, error) {
	return read(e, ctx, func(l *Ledger) ([]core.Event, error) {
		first, last, err := l.EventWindow(run, after, limit)
		if err != nil || first > last {
			return nil, err
		}
		events, err := e.backend.Events(run, first, last)
		if err != nil {
			return nil, failure(err)
		}
		return Copy(events), nil
	})
}

// AppendLog implements store.LogStore.
func (e *Engine) AppendLog(ctx context.Context, id core.JobID, data []byte) error {
	_, err := write(e, ctx, func(l *Ledger) (Change, struct{}, error) {
		change, err := l.AppendLog(id, data)
		return change, struct{}{}, err
	})
	return err
}

// ReadLog implements store.LogStore.
func (e *Engine) ReadLog(ctx context.Context, id core.JobID, offset uint64, max int) (core.LogChunk, error) {
	return read(e, ctx, func(l *Ledger) (core.LogChunk, error) {
		from, to, err := l.LogWindow(id, offset, max)
		if err != nil || from == to {
			return core.LogChunk{Next: offset}, err
		}
		data, err := e.backend.Log(id, from, to)
		if err != nil {
			return core.LogChunk{}, failure(err)
		}
		return core.LogChunk{Data: append([]byte(nil), data...), Next: to}, nil
	})
}
