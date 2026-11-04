package production

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// EnvSandbox names the variable that makes a daemon a sandbox daemon: one a user starts on purpose
// beside the daemon that holds the machine, for a test or a second checkout, in a runtime directory
// of its own. It holds no machine lock, and the daemon's status lists it.
const EnvSandbox = "VIBESYS_CLI_SANDBOX"

// claimWait bounds how long a daemon waits for the machine lock that Holder or Sandboxes of another
// process may hold for a moment.
const claimWait = 5 * time.Second

// machineDir is the user's machine directory: MachineDir when a test set it, and otherwise the one
// the user id and the machine name (runtimedir.ResolveMachine).
func (p Process) machineDir() (runtimedir.Machine, error) {
	path := p.MachineDir
	if path == "" {
		var err error
		if path, err = runtimedir.ResolveMachine(p.UID, runtimedir.System()); err != nil {
			return runtimedir.Machine{}, err
		}
	}
	return runtimedir.OpenMachine(path, p.UID, runtimedir.System())
}

// sandbox says whether this process is of a sandbox daemon, as EnvSandbox says. A sandbox daemon
// needs a runtime directory of its own: XDG_RUNTIME_DIR set, and not the runtime directory of the
// daemon that holds the machine, whose socket its commands would otherwise reach.
func (p Process) sandbox() (bool, error) {
	switch v := p.Getenv(EnvSandbox); v {
	case "":
		return false, nil
	case "1":
	default:
		return false, invalid("%s: %q is not 1; set it to 1 for a sandbox daemon, or leave it unset", EnvSandbox, v)
	}
	if rt := p.Getenv("XDG_RUNTIME_DIR"); rt == "" || !filepath.IsAbs(rt) {
		return false, invalid("%s=1 is a sandbox daemon, which needs a runtime directory of its own: set XDG_RUNTIME_DIR to an absolute directory", EnvSandbox)
	}
	m, err := p.machineDir()
	if err != nil {
		return false, err
	}
	holder, err := m.Holder()
	if err != nil {
		return false, err
	}
	if holder.GetRuntimeDir() == p.RuntimePath() {
		return false, invalid("%s=1 is a sandbox daemon, and its runtime directory %s is that of the daemon that holds this machine (pid %d); "+
			"set XDG_RUNTIME_DIR to a directory of its own", EnvSandbox, p.RuntimePath(), holder.GetPid())
	}
	return true, nil
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// machineHold is the daemon's hold on this machine: the machine lock, or a sandbox daemon's record.
type machineHold struct {
	dir    runtimedir.Machine
	self   *cliv1.MachineDaemon
	timers clock.Timers
}

var _ daemon.Machine = machineHold{}

// daemonMachine is how the daemon of the runtime directory takes this machine.
func (p Process) daemonMachine(dir runtimedir.Dir, timers clock.Timers) (machineHold, error) {
	sandbox, err := p.sandbox()
	if err != nil {
		return machineHold{}, err
	}
	m, err := p.machineDir()
	if err != nil {
		return machineHold{}, err
	}
	self := &cliv1.MachineDaemon{Pid: uint32(os.Getpid()), RuntimeDir: dir.Path(), Version: p.Version, Sandbox: sandbox}
	return machineHold{dir: m, self: self, timers: timers}, nil
}

// Claim implements daemon.Machine.
func (m machineHold) Claim(ctx context.Context) (func() error, error) {
	ctx, stop := clock.Deadline(ctx, m.timers, claimWait)
	defer stop()
	var (
		c   *runtimedir.Claim
		err error
	)
	if m.self.GetSandbox() {
		c, err = m.dir.RegisterSandbox(ctx, m.timers, m.self)
	} else {
		c, err = m.dir.Claim(ctx, m.timers, m.self)
	}
	if err != nil {
		return nil, err
	}
	return c.Release, nil
}

// Sandbox implements daemon.Machine.
func (m machineHold) Sandbox() bool { return m.self.GetSandbox() }

// Sandboxes implements daemon.Machine.
func (m machineHold) Sandboxes() ([]*cliv1.MachineDaemon, error) { return m.dir.Sandboxes() }
