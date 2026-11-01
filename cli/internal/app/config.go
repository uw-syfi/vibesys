package app

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Config is what an App is made of. Clock, Stores, Environments and Supervisors are required, and Branches for the commands that
// read or move the result branch.
type Config struct {
	Clock        clock.Clock
	Stores       Stores
	Environments environment.Environments
	Supervisors  supervisor.Supervisors
	// Branches reads and moves the result branch. Without it, compare against the branch, promote
	// and result fail as unavailable.
	Branches gitrepo.Branches

	// DefaultEnvironment is the kind a job runs in when its submission leaves the environment
	// unspecified. Until tasks declare their own, it is the host.
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
