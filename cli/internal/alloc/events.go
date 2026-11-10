package alloc

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Event is one input of Step. The agent's requests (Ask, EndAsk, RenewAsk, RenewOffAsk) and a job's
// submission (JobSubmitted) may be refused; every other event is a fact, which is never refused and
// changes nothing when it names an allocation or a job the core does not hold.
type Event interface{ event() }

// Ask is a run asking for a standing allocation of a placement, for a time limit. Accepted, it is
// a new Pending allocation and a Submit.
type Ask struct {
	Run       core.RunID
	Placement string
	For       time.Duration
}

// EndAsk is a run ending one of its allocations: at once, or with AfterJobs once the jobs it holds
// are done. Ending an allocation that has ended is accepted and changes nothing.
type EndAsk struct {
	Run       core.RunID
	ID        ID
	AfterJobs bool
}

// RenewAsk is a run giving one of its Pending or Running allocations a renewal order, replacing any
// it had.
type RenewAsk struct {
	Run   core.RunID
	ID    ID
	Order RenewalOrder
}

// RenewOffAsk is a run taking an allocation's renewal order away. It is accepted in every stage.
type RenewOffAsk struct {
	Run core.RunID
	ID  ID
}

// RunEnded is the end of a run: each of its allocations that has not ended ends at once, and the
// run asks for nothing more.
type RunEnded struct{ Run core.RunID }

// JobSubmitted is a job submitted into an allocation of its run: the one Into names, strictly, or,
// with Into empty, any running allocation it fits, else the way a job without an allocation runs.
type JobSubmitted struct {
	Run core.RunID
	Job core.JobID
	// Bound is the longest the job runs, what it would ask the backend for on its own.
	Bound time.Duration
	// Into is the allocation asked for; empty for any.
	Into ID
	// Eligible is the placements that give the job what it needs. A job runs only in an allocation
	// of one of them.
	Eligible []string
}

// JobStarted is the shell's report that a job the core started (StartJob) began to run.
type JobStarted struct{ Job core.JobID }

// JobFinished is a job that was started, or waited, in an allocation and is done there, however it
// ended, a cancel included.
type JobFinished struct{ Job core.JobID }

// Submitted is the backend taking an allocation's Submit, with its handle.
type Submitted struct {
	ID     ID
	Handle Handle
}

// SubmitRefused is the backend refusing an allocation's Submit for good: a Pending allocation the
// backend never took ends, EndExternal. One it took is unchanged. A backend that could not be
// reached is Unreachable instead, and the Submit is asked again.
type SubmitRefused struct {
	ID     ID
	Detail string
}

// Observed is what the backend says of an allocation now.
type Observed struct {
	ID   ID
	Seen Observation
}

// Unreachable is a backend that could not be asked about an allocation, or could not take its
// Submit or Release. It changes nothing: the allocation is as it was last seen.
type Unreachable struct {
	ID     ID
	Detail string
}

// Released is the backend confirming that an ended allocation's Release is done.
type Released struct{ ID ID }

// DaemonStarted is the daemon starting with an operator config: the limits of Config apply from
// this input on, until the next DaemonStarted. Every decision before it was taken under the limits
// in force then, and Fold replays it under them, so a change of the config never renumbers, drops
// or refuses anything that was accepted. An allocation of a placement that Config no longer offers
// allocations of ends at once, EndExternal, and is released. It is a fact, never refused.
type DaemonStarted struct{ Config Config }

// Tick is time passing with nothing else happening. The shell sends one now and then, so what is
// decided by time (renewals) is decided, and a Submit or Release that may not have arrived is asked
// for again.
type Tick struct{}

func (Ask) event()           {}
func (EndAsk) event()        {}
func (RenewAsk) event()      {}
func (RenewOffAsk) event()   {}
func (RunEnded) event()      {}
func (JobSubmitted) event()  {}
func (JobStarted) event()    {}
func (JobFinished) event()   {}
func (Submitted) event()     {}
func (SubmitRefused) event() {}
func (Observed) event()      {}
func (Unreachable) event()   {}
func (Released) event()      {}
func (Tick) event()          {}
func (DaemonStarted) event() {}
