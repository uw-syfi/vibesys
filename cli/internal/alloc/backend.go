package alloc

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Spec is what the shell asks a backend for: one standing allocation of a placement, for a run, with
// a time limit. The backend marks what it makes with the ID, so asking again for the same ID is the
// same allocation, and Adopt finds it again after a restart.
type Spec struct {
	ID        ID
	Run       core.RunID
	Placement string
	For       time.Duration
}

// Handle is a backend's identity of an allocation it made.
type Handle struct {
	// Alloc is the allocation the handle is for.
	Alloc ID
	// Ref is the backend's own name for it, such as a batch job's id, never empty.
	Ref string
}

// Observation is what a backend says of an allocation: SeenPending, SeenRunning or SeenEnded. An
// unreachable backend says nothing, and the shell reports Unreachable instead.
type Observation interface{ observation() }

// SeenPending is an allocation that waits to start.
type SeenPending struct {
	// Reason is the backend's word for why, when it gives one.
	Reason string
	// EstimatedStart is when it expects the start; zero when it does not say.
	EstimatedStart time.Time
}

// SeenRunning is an allocation whose capacity has started.
type SeenRunning struct {
	Node string
	// StartedAt is when it started, as the backend says; zero when it does not.
	StartedAt time.Time
	// EndsAt is when the backend will end it; zero when it does not say or it has no end.
	EndsAt time.Time
}

// SeenEnded is an allocation that is gone from the backend.
type SeenEnded struct {
	Cause EndCause
	// At is when it ended, as the backend says; zero when it does not.
	At time.Time
}

// EndCause is why a backend says an allocation ended. Whether the CLI asked for it is the core's to
// know, not the backend's.
type EndCause string

// The end causes.
const (
	// EndedByTimeLimit is the time limit the allocation was made with.
	EndedByTimeLimit EndCause = "time_limit"
	// EndedOtherwise is any other end: a cancel, by the CLI or anyone else, or a failure.
	EndedOtherwise EndCause = "otherwise"
)

func (SeenPending) observation() {}
func (SeenRunning) observation() {}
func (SeenEnded) observation()   {}
