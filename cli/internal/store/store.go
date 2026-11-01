// Package store declares Store, the logical interface to everything vibesys-cli keeps: job
// state, results, events, job logs, plans and promotions.
//
// A Store has no paths and no formats. Only internal/store/filestore knows the physical layout,
// and internal/store/memstore is an in-memory implementation for tests. Both pass the one
// contract suite this package ships, so a caller written against Store is correct for either.
package store

import (
	"context"

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
	// NoCache, a command job excepted, a queued or running job with the same Key is joined, so
	// parallel duplicate submissions make one job; and a finished job that has a reusable saved
	// result is returned as reused. The note and plan link are appended to the job it lands on.
	Submit(ctx context.Context, submission core.Submission) (core.SubmitOutcome, error)

	// Job returns one job.
	Job(ctx context.Context, id core.JobID) (core.Job, error)

	// Jobs returns the jobs matching the filter, oldest submission first.
	Jobs(ctx context.Context, filter core.JobFilter) ([]core.Job, error)

	// Transition applies a state change if the job is still in the state the transition moves
	// from, and fails with FailureConflict otherwise. A transition that carries a result saves it
	// and the new state together, so no result exists for a job that did not succeed, and an
	// interrupted measure leaves none. Whether the move is legal is core's decision.
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
