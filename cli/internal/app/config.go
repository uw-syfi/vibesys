package app

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Retry says how long a call that keeps failing is tried.
type Retry struct {
	// Attempts is how many times a call is made.
	Attempts int
	// First is the wait after the first failure. Each wait doubles, up to Longest.
	First, Longest time.Duration
}

// Scheduler is how the daemon shares the machine between jobs: the resources jobs claim, the policy
// that orders the jobs that wait, the filters that may hold one back, and the demand that says what
// each job claims. With no resources the machine runs one job at a time (lifecycle.OneAtATime), and
// with no demand each job claims as schedule.DefaultDemand says.
type Scheduler struct {
	Resources []schedule.Resource
	Policy    schedule.Policy
	Filters   []schedule.Filter
	Demand    schedule.Demand
}

func (s Scheduler) machine() lifecycle.Scheduling {
	return lifecycle.Scheduling{Resources: s.Resources, Policy: s.Policy, Filters: s.Filters}
}

// Config is what an App is made of. Clock, Stores, Environments and Supervisors are required, and
// Branches for the commands that read or move the result branch.
type Config struct {
	Clock clock.Clock
	// Timers is what every wait of the App goes through. It defaults to the system's.
	Timers       clock.Timers
	Stores       Stores
	Environments environment.Environments
	Supervisors  supervisor.Supervisors
	// Repositories resolves the directory a caller sent to the repository its store is keyed by. It
	// is required: without it every call that names a task fails as unavailable.
	Repositories gitrepo.Resolver
	// Branches reads and moves the result branch. Without it, compare against the branch, promote
	// and result fail as unavailable.
	Branches gitrepo.Branches

	// Tasks reads a task as a commit has it. It is required to run check and measure jobs, which
	// are refused as unavailable without it; command jobs do not read a task.
	Tasks task.Source

	// Scheduler is how the machine is shared between jobs. The zero value runs one job at a time,
	// the oldest first.
	Scheduler Scheduler
	// Leasing is how the resources jobs hold alone are shared with other daemons. The zero value
	// leases nothing.
	Leasing Leasing

	// Placements are the placements of the operator config a docker job can run on, with the kinds
	// of resource each gives. A docker job runs on the one it asks for, or on the one that gives
	// what its task needs (core.ChoosePlacement). Empty leaves a job's request as it is.
	Placements []core.PlacementOffer

	// DefaultTimeout is the time limit of a command that sets none.
	DefaultTimeout time.Duration
	// MaxNoteBytes bounds the text of a note.
	MaxNoteBytes int
	// Retry bounds the calls to stores and supervisors.
	Retry Retry
	// CallTimeout bounds one call to a store, and one to a supervisor that answers at once, when
	// all is well.
	CallTimeout time.Duration
	// StopTimeout bounds the wait for a stopped command to end.
	StopTimeout time.Duration
	// SlowPeriod is how often the daemon looks again at what it could not settle: a job in doubt,
	// an end its store refused, a store that was away, a submission that reached a store and
	// never the machine.
	SlowPeriod time.Duration
	// LogBatchBytes and LogBatchDelay say when a job's output is written to its store: when a
	// batch is this big, or when its first byte has waited this long.
	LogBatchBytes int
	LogBatchDelay time.Duration
	// Epoch names this boot of the daemon in the streams that report a task's state: a client
	// resumes only within one. It defaults to a name made from the time the App started.
	Epoch string
	// Logf receives the daemon's own diagnostics: what it could not do and will retry. It may be
	// nil.
	Logf func(format string, args ...any)
}

// Defaults for the fields of Config that have one.
const (
	DefaultCommandTimeout = 30 * time.Minute
	DefaultMaxNoteBytes   = 4096
	DefaultCallTimeout    = 30 * time.Second
	DefaultStopTimeout    = 5 * time.Minute
	DefaultSlowPeriod     = 30 * time.Second
	DefaultLogBatchBytes  = 64 << 10
	DefaultLogBatchDelay  = time.Second
)

// DefaultRetry tries for about a minute.
var DefaultRetry = Retry{Attempts: 8, First: 500 * time.Millisecond, Longest: 30 * time.Second}

func (c Config) withDefaults() Config {
	if c.DefaultTimeout <= 0 {
		c.DefaultTimeout = DefaultCommandTimeout
	}
	if c.MaxNoteBytes <= 0 {
		c.MaxNoteBytes = DefaultMaxNoteBytes
	}
	if c.Scheduler.Demand == nil {
		c.Scheduler.Demand = schedule.DefaultDemand{}
	}
	if c.Timers == nil {
		c.Timers = clock.System{}
	}
	if c.Retry.Attempts <= 0 {
		c.Retry = DefaultRetry
	}
	if c.CallTimeout <= 0 {
		c.CallTimeout = DefaultCallTimeout
	}
	if c.StopTimeout <= 0 {
		c.StopTimeout = DefaultStopTimeout
	}
	if c.SlowPeriod <= 0 {
		c.SlowPeriod = DefaultSlowPeriod
	}
	if c.LogBatchBytes <= 0 {
		c.LogBatchBytes = DefaultLogBatchBytes
	}
	if c.LogBatchDelay <= 0 {
		c.LogBatchDelay = DefaultLogBatchDelay
	}
	if c.Logf == nil {
		c.Logf = func(string, ...any) {}
	}
	return c
}
