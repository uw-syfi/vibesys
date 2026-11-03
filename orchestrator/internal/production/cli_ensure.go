package production

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
)

// ensureArgv pins the only command used to start the shared CLI daemon.
const ensureArgv = "daemon ensure"

// Runner runs an argv with an explicit environment and returns only its status.
type Runner interface {
	Run(context.Context, string, []string, []string) error
}
type ExecRunner struct{}

func (ExecRunner) Run(ctx context.Context, program string, args, env []string) error {
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Env = env
	return cmd.Run()
}

// ExecEnsurer runs the recorded absolute CLI binary, never a PATH lookup.
type ExecEnsurer struct {
	Executable string
	Runner     Runner
}

func (e ExecEnsurer) Ensure(ctx context.Context, loc clidaemon.Location) error {
	if _, ok := ctx.Deadline(); !ok {
		return errors.New("ensure requires a deadline")
	}
	binary := e.Executable
	if !filepath.IsAbs(binary) {
		return &clidaemon.Unreadable{Reason: clidaemon.CLINotInstalled, Detail: "recorded CLI executable must be absolute"}
	}
	env := []string{}
	for _, v := range os.Environ() {
		if !strings.HasPrefix(v, "HOME=") && !strings.HasPrefix(v, "XDG_RUNTIME_DIR=") && !strings.HasPrefix(v, "XDG_STATE_HOME=") {
			env = append(env, v)
		}
	}
	for name, value := range map[string]string{"HOME": loc.Home, "XDG_RUNTIME_DIR": loc.XDGRuntimeDir, "XDG_STATE_HOME": loc.XDGStateHome} {
		if value != "" {
			env = append(env, name+"="+value)
		}
	}
	err := e.Runner.Run(ctx, binary, strings.Fields(ensureArgv), env)
	if errors.Is(err, os.ErrNotExist) {
		return &clidaemon.Unreadable{Reason: clidaemon.CLINotInstalled, Detail: err.Error()}
	}
	return err
}
