// Package store declares Store, the logical interface to everything vibesys-cli keeps: job
// state, results, events, job logs, plans and promotions.
//
// A Store has no paths and no formats. Only internal/store/filestore knows the physical layout,
// and internal/store/memstore is an in-memory implementation for tests. Both pass the one
// contract suite this package ships, so a caller written against Store is correct for either.
package store

import (
	"context"
	"errors"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Store is one task's state in one repository. The daemon is its only user at run time, so
// implementations need to be safe for concurrent use inside one process and atomic against a
// process that dies at any point.
//
// Every method that changes state is one all-or-nothing change: after a crash the change is
// there in full or not at all, and no earlier state is lost. Decisions (what may transition,
// what a key is, what a verdict says) belong to internal/core, and the Store only stores them.
// What it does decide is atomicity, so an operation takes whatever must change together.
//
// A method fails with a *core.Failure: FailureNotFound for an unknown job, plan or log,
// FailureConflict when a precondition no longer holds, FailureInvalidInput for a value the
// store cannot hold, and FailureInternal for anything else. A cancelled context fails with the
// context's error and changes nothing.
//
// Values. Every implementation holds the same values, and a value it cannot hold is rejected
// whole, with FailureInvalidInput naming the field, before anything changes:
//
//   - A string is valid UTF-8 and a float is finite, so what comes back is what went in.
//   - A time is stored and returned in UTC, to the nanosecond, in years 0 to 9999. An empty slice
//     or map comes back nil, and an interface field holds a struct value, never a pointer.
//   - A job key and a run id are names: 1 to 128 of A-Z, a-z, 0-9, dot, underscore and hyphen,
//     not starting with a dot. A command job has no key and every other job has one.
//   - The store assigns job ids ("j1", "j2", ...) and plan numbers (1, 2, ...) in order. Neither
//     is ever reused.
//
// Values come back as copies: changing one never changes the store.
type Store interface {
	JobStore
	ResultStore
	PlanStore
	EventStore
	LogStore
}

// JobStore holds job state.
type JobStore interface {
	// Submit queues a job, or lands on an existing one, as one change. Unless the submission sets
	// NoCache, a command job excepted, the oldest queued or running job that core.CanJoin accepts
	// is joined, so parallel duplicate submissions make one job; and a finished job that has a
	// reusable saved result is returned as reused. The note and plan link are appended to the job
	// it lands on.
	Submit(ctx context.Context, submission core.Submission) (core.SubmitOutcome, error)

	// Job returns one job.
	Job(ctx context.Context, id core.JobID) (core.Job, error)

	// Jobs returns the jobs matching the filter, oldest submission first.
	Jobs(ctx context.Context, filter core.JobFilter) ([]core.Job, error)

	// Transition applies a state change if the job is still in the state the transition moves
	// from, and fails with FailureConflict otherwise. Whether the move is legal, and which effects
	// go with it, is the decision of core.Job.Apply, and a move it refuses changes nothing and
	// fails as it does. A transition that carries a result saves it and the new state together,
	// so no result exists for a job that did not succeed, and an interrupted measure leaves none.
	Transition(ctx context.Context, transition core.Transition) (core.Job, error)

	// AddNote appends a note to a job in any state.
	AddNote(ctx context.Context, id core.JobID, note core.Note) (core.Job, error)
}

// ResultStore reads saved results and keeps the promotion record they are judged against.
// Results are written only through JobStore.Transition.
type ResultStore interface {
	// Results returns the results matching the filter, oldest first. An empty answer is not an
	// error.
	Results(ctx context.Context, filter core.ResultFilter) ([]core.Result, error)

	// RecordPromotion appends a promotion to the record. The record is append-only.
	RecordPromotion(ctx context.Context, promotion core.Promotion) error

	// Promotions returns every recorded promotion, oldest first.
	Promotions(ctx context.Context) ([]core.Promotion, error)
}

// PlanStore holds plans.
type PlanStore interface {
	// CreatePlan stores a plan with the next number, open, and returns it. Numbers are never
	// reused.
	CreatePlan(ctx context.Context, plan core.NewPlan) (core.Plan, error)

	// Plan returns one plan, comments included.
	Plan(ctx context.Context, number core.PlanNumber) (core.Plan, error)

	// Plans returns the plans matching the filter, by number.
	Plans(ctx context.Context, filter core.PlanFilter) ([]core.Plan, error)

	// EditPlan applies an edit, including a status change and a comment, as one change.
	EditPlan(ctx context.Context, number core.PlanNumber, edit core.PlanEdit) (core.Plan, error)
}

// EventStore holds the append-only event log. Events are a record for later features: nothing
// reads them to decide anything. An event is appended after the change it describes, so a crash
// between the two can lose the event but never the change.
type EventStore interface {
	// AppendEvent gives the draft the next position in its run's log and stores it. Positions
	// start at 1 and have no gaps.
	AppendEvent(ctx context.Context, draft core.EventDraft) (core.Event, error)

	// Events returns up to limit events of a run with a position above after, in order. A limit
	// of zero means no limit.
	Events(ctx context.Context, run core.RunID, after uint64, limit int) ([]core.Event, error)
}

// LogStore holds each job's output.
type LogStore interface {
	// AppendLog appends bytes to a job's log. Bytes appended before a crash stay.
	AppendLog(ctx context.Context, id core.JobID, data []byte) error

	// ReadLog returns up to max bytes of a job's log from a byte offset, and where the next read
	// continues. An offset at the end returns no data and the same offset. A max of zero means
	// no limit.
	ReadLog(ctx context.Context, id core.JobID, offset uint64, max int) (core.LogChunk, error)
}

// ErrOwnerLeaving marks a refused store whose holder has the caller's own place: the same machine
// and runtime directory, which have one daemon. So it is a daemon on its way out, whose descriptors
// the kernel is still closing, and trying again for a bounded time is the answer.
var ErrOwnerLeaving = errors.New("the owner of the store is leaving")

// Rules are the decisions of internal/core that depend on the task, which the daemon passes to an
// implementation when it opens the store. The zero Rules is a store for a task that declares
// nothing.
type Rules struct {
	// Reuse decides whether the saved results answer a submission with the same key. A nil Reuse
	// lands on the latest saved result with the key, which is right when the key already covers
	// everything a result depends on. A key does not cover how many samples a measurement
	// holds, so a daemon that tops up measurements passes a Reuse built from core.PlanCache.
	Reuse ReusePolicy
	// DefaultSamples is the task's default sample count, the number of samples a measure
	// submission asks for when its settings leave Samples at zero. core.CanJoin needs it to say
	// whether a job in flight takes as many samples as a submission asks for; with the zero value
	// a submission that leaves Samples at zero joins nothing.
	DefaultSamples uint32
}

// ReusePolicy decides whether saved results answer a submission. It is given every result saved
// under the submission's key, in the order they were saved, because together they can answer what
// none does alone: a measurement and its top-up. It returns the job, one of those results', that
// the submission lands on. The rule belongs to internal/core.
type ReusePolicy func(saved []core.Result, want core.Submission) (core.JobID, bool)
