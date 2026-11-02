package app

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Config is what an App is made of. Clock, Stores, Environments and Supervisors are required.
type Config struct {
	Clock        clock.Clock
	Stores       Stores
	Environments environment.Environments
	Supervisors  supervisor.Supervisors

	// Tasks reads a task as a commit has it. It is required to run check and measure jobs, which
	// are refused as unavailable without it; command jobs do not read a task.
	Tasks task.Source

	// DefaultEnvironment is the kind a command job runs in when its submission leaves the
	// environment unspecified. A check or a measure job's environment is decided by its task
	// (core.ChooseEnvironment).
	DefaultEnvironment core.EnvironmentKind
	// DefaultTimeout is the time limit of a command that sets none.
	DefaultTimeout time.Duration
	// MaxNoteBytes bounds the text of a note.
	MaxNoteBytes int
	// Logf receives the daemon's own diagnostics: what it could not do and will retry on its next
	// start. It may be nil.
	Logf func(format string, args ...any)
}

// Defaults for the fields of Config that have one.
const (
	DefaultCommandTimeout = 30 * time.Minute
	DefaultMaxNoteBytes   = 4096
)

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
	if c.Logf == nil {
		c.Logf = func(string, ...any) {}
	}
	return c
}
