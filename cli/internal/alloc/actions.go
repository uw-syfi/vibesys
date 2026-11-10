package alloc

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Action is what Step asks the shell to do. The shell carries each out and reports what came of it
// as an Event: Submit with Submitted, SubmitRefused or Unreachable; Release with Released or
// Unreachable; StartJob with JobStarted and then JobFinished.
type Action interface{ action() }

// Submit asks the backend for the allocation the spec describes. It is idempotent by Spec.ID, and
// asked again at every Tick until the backend took it.
type Submit struct{ Spec Spec }

// Release asks the backend to end an allocation and give its capacity back. It is idempotent, and
// asked again at every Tick until the backend confirmed it.
type Release struct {
	ID     ID
	Handle Handle
}

// StartJob asks the shell to start a job in an allocation. It fits there now (Fit), and nothing
// else runs there.
type StartJob struct {
	Job   core.JobID
	ID    ID
	Bound time.Duration
}

// LoseJob is a job that was running in an allocation when it ended: the job is lost, for the
// allocation's end reason.
type LoseJob struct {
	Job    core.JobID
	ID     ID
	Reason EndReason
}

// DropJob is a job submitted strictly into an allocation that will never run there: the allocation
// ended before it started (NotRunning), or it no longer fits (DoesNotFit). The job fails with the
// reason.
type DropJob struct {
	Job    core.JobID
	ID     ID
	Reason Refusal
}

// NotPlaced is a job submitted to any allocation of its run that none takes: it runs the way a job
// submitted without an allocation does. Why is the refusal that came closest to taking it.
type NotPlaced struct {
	Job core.JobID
	Why Refusal
}

// RecordWait is how long an allocation waited from its request to its start, a fact for
// placement capacity.
type RecordWait struct {
	Placement string
	Sample    WaitSample
}

// WaitSample is one allocation's wait.
type WaitSample struct {
	RequestedAt  time.Time
	Waited       time.Duration
	RequestedFor time.Duration
}

// Refuse is a request Step refused. Nothing changed.
type Refuse struct {
	Event  Event
	Reason Refusal
}

func (Submit) action()     {}
func (Release) action()    {}
func (StartJob) action()   {}
func (LoseJob) action()    {}
func (DropJob) action()    {}
func (NotPlaced) action()  {}
func (RecordWait) action() {}
func (Refuse) action()     {}
