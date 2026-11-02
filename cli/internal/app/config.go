package app

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Retry says how long a call that keeps failing is tried.
type Retry struct {
	// Attempts is how many times a call is made.
	Attempts int
	// First is the wait after the first failure. Each wait doubles, up to Longest.
	First, Longest time.Duration
}

// Config is what an App is made of. Clock, Stores, Environments and Supervisors are required.
type Config struct {
	Clock clock.Clock
	// Timers is what every wait of the App goes through. It defaults to the system's.
	Timers       clock.Timers
	Stores       Stores
	Environments environment.Environments
	Supervisors  supervisor.Supervisors

	// DefaultEnvironment is the kind a job runs in when its submission leaves the environment
	// unspecified. Until tasks declare their own, it is the host.
	DefaultEnvironment core.EnvironmentKind
	// DefaultTimeout is the time limit of a command that sets none.
	DefaultTimeout time.Duration
	// MaxNoteBytes bounds the text of a note.
	MaxNoteBytes int
	// Retry bounds the calls to stores and supervisors.
	Retry Retry
	// CallTimeout bounds one call to a supervisor that answers at once, when all is well.
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
	if c.DefaultEnvironment == core.EnvironmentAuto {
		c.DefaultEnvironment = core.EnvironmentHost
	}
	if c.DefaultTimeout <= 0 {
		c.DefaultTimeout = DefaultCommandTimeout
	}
	if c.MaxNoteBytes <= 0 {
		c.MaxNoteBytes = DefaultMaxNoteBytes
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
