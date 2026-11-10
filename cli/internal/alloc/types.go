package alloc

import (
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// ID names one standing allocation, such as a7. The core numbers them in the order it makes them,
// across every run, so an ID names one allocation for as long as the daemon's log is kept.
type ID string

// Valid reports whether the ID is "a" and a number from 1, as the core makes them.
func (id ID) Valid() bool {
	n, ok := id.number()
	return ok && n > 0
}

// number is the ID's number, and false for an ID the core never makes.
func (id ID) number() (uint64, bool) {
	digits, ok := strings.CutPrefix(string(id), "a")
	if !ok || digits == "" || digits[0] == '0' {
		return 0, false
	}
	n, err := strconv.ParseUint(digits, 10, 64)
	return n, err == nil
}

func idOf(n uint64) ID { return ID("a" + strconv.FormatUint(n, 10)) }

// Stage is where an allocation is: the state of AllocationState in allocation.proto.
type Stage string

// The stages, in the only order an allocation moves through them.
const (
	StagePending  Stage = "pending"
	StageRunning  Stage = "running"
	StageDraining Stage = "draining"
	StageEnded    Stage = "ended"
)

// rank orders the stages: an allocation's rank never goes down.
func (s Stage) rank() int {
	switch s {
	case StagePending:
		return 0
	case StageRunning:
		return 1
	case StageDraining:
		return 2
	case StageEnded:
		return 3
	}
	return -1
}

// EndReason is why an allocation ended.
type EndReason string

// The end reasons.
const (
	// EndRequested is the run's own end: alloc end, the last job of a draining allocation, or the
	// end of the run.
	EndRequested EndReason = "requested"
	// EndTimeLimit is the time limit the allocation was asked for, as the backend enforced it.
	EndTimeLimit EndReason = "time_limit"
	// EndQueueTimeout is a pending allocation that waited longer than its placement's queue timeout.
	EndQueueTimeout EndReason = "queue_timeout"
	// EndExternal is an end the CLI did not ask for, such as an administrator's cancel, or a
	// submission the backend refused.
	EndExternal EndReason = "external"
)

// Phase is an allocation's stage and the facts that only that stage has. It is one of Pending,
// Running, Draining and Ended.
type Phase interface {
	// Stage is the phase's stage.
	Stage() Stage
	phase()
}

// Pending is an allocation that was asked for and has not started.
type Pending struct {
	// Reason is the backend's word for why it waits, when it gave one.
	Reason string
	// EstimatedStart is when the backend expects it to start; zero when it does not say.
	EstimatedStart time.Time
}

// Live is what a started allocation that has not ended has: when it started and ends, and the jobs
// it holds.
type Live struct {
	StartedAt time.Time
	// EndsAt is when it ends by its time limit, at most StartedAt + RequestedFor. Zero for a
	// placement whose allocations have no end (Limits.Endless), where every job fits.
	EndsAt time.Time
	// Node is where it runs, when the backend says.
	Node string
	// Current is the job running in it, nil when none is.
	Current *Slot
	// Queue is the jobs that wait to run in it, oldest first.
	Queue []Slot
}

// Running is an allocation whose capacity has started, which takes jobs.
type Running struct{ Live }

// Draining is an allocation asked to end once the jobs it holds are done. It takes no new job.
type Draining struct{ Live }

// Ended is an allocation that ended. It is final.
type Ended struct {
	At     time.Time
	Reason EndReason
	// StartedAt is when it started; zero for one that never did.
	StartedAt time.Time
	// Released says nothing of it is left at the backend: the backend ended it, or confirmed the
	// release the core asked for. Until then the shell is asked again, at every Tick, to release it.
	Released bool
}

// Stage implements Phase.
func (Pending) Stage() Stage { return StagePending }

// Stage implements Phase.
func (Running) Stage() Stage { return StageRunning }

// Stage implements Phase.
func (Draining) Stage() Stage { return StageDraining }

// Stage implements Phase.
func (Ended) Stage() Stage { return StageEnded }

func (Pending) phase()  {}
func (Running) phase()  {}
func (Draining) phase() {}
func (Ended) phase()    {}

// Slot is one job held by an allocation.
type Slot struct {
	Job core.JobID
	// Bound is the longest the job runs: what it would ask the backend for if it ran on its own.
	Bound time.Duration
	// Any says it was submitted to any allocation of its run, so where it does not fit it runs the
	// way a job submitted without an allocation does, instead of being dropped.
	Any bool
	// StartedAt is when the shell reported it started; zero until then, while its whole bound is
	// still ahead.
	StartedAt time.Time
}

// SkipReason is why a renewal order that was due asked for no successor. It is decided again at
// every step and cleared once it no longer holds.
type SkipReason string

// The skip reasons.
const (
	// SkipMaxHeld: the run holds max_held allocations of the placement.
	SkipMaxHeld SkipReason = "max_held reached"
	// SkipPastUntil: the order's until has passed.
	SkipPastUntil SkipReason = "past the order's until"
	// SkipNotEnabled: the placement no longer offers standing allocations.
	SkipNotEnabled SkipReason = "the placement offers no standing allocations"
	// SkipForExceedsMaxFor: the order's successor_for is longer than max_for now allows.
	SkipForExceedsMaxFor SkipReason = "successor_for exceeds max_for"
)

// Until is when a renewal order stops asking for successors: UntilTime or UntilRunEnd.
type Until interface{ until() }

// UntilTime stops the order at an instant: no successor is asked for at or after At.
type UntilTime struct{ At time.Time }

// UntilRunEnd keeps the order until the run ends.
type UntilRunEnd struct{}

func (UntilTime) until()   {}
func (UntilRunEnd) until() {}

// RenewalOrder asks for a successor of For on the same placement once the allocation's time left is
// at most Lead. The successor carries the same order. The old one runs to its end.
type RenewalOrder struct {
	Lead  time.Duration
	For   time.Duration
	Until Until
}

// Allocation is the record of one standing allocation.
type Allocation struct {
	ID        ID
	Placement string
	Run       core.RunID
	// RequestedFor is its time limit from its start.
	RequestedFor time.Duration
	RequestedAt  time.Time
	// Handle is the backend's identity of it, the zero Handle until the backend took the request.
	Handle Handle
	Phase  Phase
	// Renewal is its standing renewal order, nil for none.
	Renewal *RenewalOrder
	// RenewedBy is the successor its order made; empty for none. It is set at most once.
	RenewedBy ID
	// RenewalSkipped is why its order, due now, made no successor; empty otherwise.
	RenewalSkipped SkipReason
}

// Stage is the allocation's stage.
func (a Allocation) Stage() Stage { return a.Phase.Stage() }

// live is the allocation's Live part, and false for one that has none.
func (a Allocation) live() (Live, bool) {
	switch p := a.Phase.(type) {
	case Running:
		return p.Live, true
	case Draining:
		return p.Live, true
	case Pending, Ended:
	}
	return Live{}, false
}

// holds says the allocation counts towards its run's max_held: it is not Ended.
func (a Allocation) holds() bool { return a.Stage() != StageEnded }

// clone is a copy that shares nothing mutable with a.
func (a Allocation) clone() Allocation {
	switch p := a.Phase.(type) {
	case Running:
		p.Live = p.Live.clone()
		a.Phase = p
	case Draining:
		p.Live = p.Live.clone()
		a.Phase = p
	case Pending, Ended:
	}
	if a.Renewal != nil {
		r := *a.Renewal
		a.Renewal = &r
	}
	return a
}

func (l Live) clone() Live {
	if l.Current != nil {
		c := *l.Current
		l.Current = &c
	}
	l.Queue = slices.Clone(l.Queue)
	return l
}

// Limits are what the operator config says of one placement's standing allocations, and how the
// daemon judges them.
type Limits struct {
	// MaxHeld is how many allocations one run may hold on the placement at once (Pending, Running
	// and Draining), renewals included.
	MaxHeld int
	// MaxFor is the longest an allocation may be asked for.
	MaxFor time.Duration
	// Margin is the time a job's fit keeps free before the allocation's end.
	Margin time.Duration
	// QueueTimeout, when positive, ends a pending allocation observed still waiting this long after
	// it was asked for.
	QueueTimeout time.Duration
	// Endless says the placement's allocations have no end, such as a slot of this machine: they
	// never get an EndsAt, every job fits, and a renewal order is refused.
	Endless bool
}

// Config is the limits of every placement that offers standing allocations, by name. A placement
// that is not here offers none.
type Config struct {
	Placements map[string]Limits
}

// ConfigKey is the operator config section that enables standing allocations of a placement.
func ConfigKey(placement string) string { return "placements." + placement + ".allocations" }
