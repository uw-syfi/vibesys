// Package store is the orchestrator's durable record.
//
// The core decides and the shell commits: each Decide result goes in through Commit as one atomic
// step that writes the events, the tables the screens read, the receipt of a command and the
// effects still to carry out. Events are written before the effects they cause, in the same
// transaction, so a crash leaves either all of a result or none of it, and an effect never exists
// without the fact that asked for it.
//
// The events are the truth. Recover rebuilds the core's State from them with core.Replay, and every
// table here is derived from them: Verify checks that the tables are what a replay gives.
//
// Two implementations pass one contract suite, storetest.Contract: Memory, the Fake the daemon's
// own tests use, and SQLite, which keeps everything in one local file. A Store has one writer at a
// time and any number of readers; every method may be called from any goroutine.
//
//   - store.go: the interface, its types and its limits.
//   - tables.go: the pure projection of events into tables, shared by both implementations.
//   - memory.go: the Fake.
//   - sqlite.go, schema.go: the file-backed store.
//   - storetest: the contract suite.
package store

import (
	"errors"
	"fmt"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The page limits a reader may not exceed. A page holds at most MaxPageItems items and at most
// MaxPageBytes of their encoded form, except that a page always holds one item, however large, so
// that a reader can always make progress.
const (
	MaxPageItems = 75
	MaxPageBytes = 1 << 20
	// MaxPageEvents and MaxPageMessages bound the other paged reads.
	MaxPageEvents   = 500
	MaxPageMessages = 200
	// MaxWindowTurns bounds what a timeline window returns.
	MaxWindowTurns = 2000
	// MaxFinishedRuns is how many finished runs a Snapshot lists, the most recently changed first
	// chosen. Every live run is listed. A snapshot is what a client loads on every connect, so it
	// must not grow with the age of the installation.
	MaxFinishedRuns = 100
)

// ErrCompacted says the events a reader asked for were dropped by Compact. The reader takes a
// Snapshot and follows from its sequence.
var ErrCompacted = errors.New("store: events before the requested sequence were compacted")

// ErrAheadOfLog says a reader's cursor is above the end of the log. A store under synchronous=NORMAL
// can lose its last commits to a power cut, so a client may hold a sequence the log no longer
// reaches. The reader's picture is then ahead of the store and cannot be continued: the daemon
// answers with a fresh Snapshot and the client starts again from its sequence.
var ErrAheadOfLog = errors.New("store: the cursor is above the end of the log")

// Result is what one Decide produced, and what Commit makes durable.
type Result struct {
	Events  []core.Event
	Effects []core.Effect
}

// Committed says what a commit assigned.
type Committed struct {
	// First and Last are the sequences of the result's events: gap-free, increasing, and zero when
	// the result held no events.
	First, Last uint64
}

// Store is the durable record.
type Store interface {
	Reader

	// Commit makes a result durable as one step, or fails and leaves the store as it was. Events
	// get the next sequences in order, a receipt in them is keyed by its command and carries its
	// own event's sequence, and each effect is queued in the outbox after them. A result with
	// effects and no events is refused: an effect is always the consequence of a fact.
	Commit(Result) (Committed, error)

	// Recover returns the state the committed events give, as core.Replay folds them. It is what
	// a daemon starts from, and costs a checkpoint load plus the events since.
	Recover() (core.State, error)

	// Claim hands out up to limit effects that are ready at now and not already claimed, oldest
	// first. An effect with a time is ready from then on, which is how a timer survives a restart.
	// A claimed effect stays in the outbox until Complete, and a store reopened after a crash
	// offers it again, so delivery is at least once.
	Claim(now time.Time, limit int) ([]Claimed, error)
	// Complete removes claimed effects for good. An unknown id is not an error: completing twice
	// is harmless.
	Complete(ids ...OutboxID) error
	// Release returns claimed effects to the outbox, for a retry.
	Release(ids ...OutboxID) error
	// Reclaim returns every claimed effect to the outbox and says how many there were, for a shell
	// that lost the executors of its claims without restarting (a reopened store does the same).
	// An effect it returns is offered again by Claim, so delivery stays at least once.
	Reclaim() (int, error)
	// NextDue is the earliest time an unclaimed timed effect becomes ready, so a shell knows when
	// to look again. It says nothing of effects that are ready now.
	NextDue() (time.Time, bool, error)

	// Compact drops what recovery and the screens no longer need, without changing what Recover
	// returns. See Retention. It takes time proportional to the state, but holds the writer only
	// for short steps, so commits go on while it runs; two calls run one after the other.
	Compact(Retention, time.Time) (Compaction, error)

	// Verify checks the store against itself, on one consistent snapshot, beside the writer: the
	// sequences have no gap; every row of every table carries in its index columns what its body
	// says, and equals the last retained event that wrote it, or has no retained event because
	// Compact dropped its run's events; every entity the retained events write has its row; and the
	// runs, agents, messages in transit and receipts equal what Recover returns. It reads
	// everything, so it is for startup and tests.
	Verify() error

	// Close releases the store. It is idempotent.
	Close() error
}

// Reader is what the screens read. Every read sees one consistent commit.
type Reader interface {
	// Snapshot returns every live run, the MaxFinishedRuns finished runs that changed last, and the
	// agents of those runs, with the sequence of the last event it includes. A client follows with
	// EventsAfter from that sequence.
	Snapshot() (Snapshot, error)

	// EventsAfter returns up to limit events with a sequence above after, oldest first, for one
	// run, or for every run when run is empty. Events of a finished run may have been compacted:
	// that is ErrCompacted, when after is below the last sequence the run dropped. A cursor above
	// the end of the log is ErrAheadOfLog, not an empty page.
	EventsAfter(run core.RunID, after uint64, limit int) ([]EventRecord, error)

	// Items returns a page of one agent's items. A zero before means the newest; otherwise the
	// page holds items with an ordinal below before. Items come in ascending ordinal, and the
	// page's first ordinal is the next before. limit is capped at MaxPageItems.
	Items(agent core.AgentID, before uint32, limit int) (ItemPage, error)

	// Messages returns up to limit of a run's messages with a Seq above after, oldest first. A
	// cursor above the end of the log is ErrAheadOfLog.
	Messages(run core.RunID, after uint64, limit int) ([]MessageRecord, error)

	// Turns returns the turns of a run that overlap [from, to]: started before to, and still
	// running or ended after from. They come in start order.
	Turns(run core.RunID, from, to time.Time, limit int) (TurnWindow, error)
}

// Snapshot is the runs and agents as of a sequence.
type Snapshot struct {
	Sequence uint64
	Runs     []core.Run
	Agents   []core.Agent
}

// EventRecord is a committed event with its sequence.
type EventRecord struct {
	Sequence uint64
	Event    core.Event
}

// ItemPage is a page of items.
type ItemPage struct {
	Items []core.Item
	// HasOlder says an item with a lower ordinal exists.
	HasOlder bool
}

// MessageRecord is a message with its place in the order messages were created, which does not
// change when its delivery does.
type MessageRecord struct {
	Seq     uint64
	Message core.Message
}

// TurnWindow is the turns in a window.
type TurnWindow struct {
	Turns []core.Turn
	// Truncated says there were more than the limit, and the latest are missing.
	Truncated bool
}

// OutboxID names one queued effect in the outbox. It is the store's, not the effect's: a repeated
// effect has two OutboxIDs and one core.EffectID.
type OutboxID uint64

// Claimed is an effect handed out for carrying out.
type Claimed struct {
	ID     OutboxID
	Effect core.Effect
}

// Retention says what Compact may drop. Nothing it drops changes what Recover returns or what a
// Reader returns for a live run.
//
// Retention covers the event log and the checkpoint, and nothing else. It drops the events of a
// finished run, after which EventsAfter says ErrCompacted for that run, and it replaces the
// checkpoint. It never drops the rows of the tables: a finished run's agents, turns, items and
// messages stay readable through Items, Messages and Turns for as long as the file lives, because
// those are the history the screens show of a past run. Their growth is the cost of that, and
// bounding it is a separate decision (it needs an archive or a deletion, and a user-facing rule for
// which runs go); Snapshot already bounds what a client loads at connect. Verify accepts exactly
// this shape: a row with no retained event must belong to a run that was pruned past the row's
// last write.
type Retention struct {
	// CheckpointAfter is how many events may follow the last checkpoint before Compact writes a new
	// one, which bounds the work of Recover. Zero means a checkpoint on every Compact.
	CheckpointAfter uint64
	// KeepEvents is how long the events of a finished run, and of a command refused before it
	// reached a run, stay readable. Zero keeps them until the next Compact, which is the shortest
	// any caller can want.
	KeepEvents time.Duration
}

// Compaction reports what a Compact did.
type Compaction struct {
	// Checkpoint is the sequence of the checkpoint now in force.
	Checkpoint uint64
	// Checkpointed says this call wrote it.
	Checkpointed bool
	// EventsDropped counts events removed from the log.
	EventsDropped int
}

// Corrupt says the store holds something that contradicts itself. It names what.
type Corrupt struct{ Detail string }

func (c Corrupt) Error() string { return "store: corrupt: " + c.Detail }

func corruptf(format string, args ...any) error { return Corrupt{Detail: fmt.Sprintf(format, args...)} }

// pageLimit applies a reader's limit and the cap for its kind.
func pageLimit(limit, max int) int {
	if limit <= 0 || limit > max {
		return max
	}
	return limit
}
