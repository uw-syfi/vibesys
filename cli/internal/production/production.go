package production

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/filestores"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/host"
)

// Process is what the binary knows of itself and of the process it runs in.
type Process struct {
	// Version is the release version of the build.
	Version string
	// Executable is the path of the running binary, which starts the daemon and the supervisors.
	Executable string
	Getenv     func(string) string
	UID        int
	Home       string
}

// RuntimeDir opens the user's runtime directory.
func (p Process) RuntimeDir() (runtimedir.Dir, error) {
	return runtimedir.Open(runtimedir.Resolve(p.Getenv, p.UID), p.UID, runtimedir.System())
}

// ClientOptions returns how a client reaches the daemon, starting it from this binary when none
// runs.
func (p Process) ClientOptions() (client.Options, error) {
	dir, err := p.RuntimeDir()
	if err != nil {
		return client.Options{}, err
	}
	return client.Options{
		Rendezvous: client.LocalRendezvous{Dir: dir}, Version: p.Version,
		Starter: client.ExecStarter{Dir: dir, Command: func() *exec.Cmd {
			return exec.Command(p.Executable, "daemon", "run")
		}},
	}, nil
}

// RunDaemon is the daemon process: `vibesys-cli daemon run`. ready is the readiness pipe a
// client that started the daemon passed.
func (p Process) RunDaemon(ctx context.Context, ready io.WriteCloser) error {
	dir, err := p.RuntimeDir()
	if err != nil {
		return err
	}
	index, err := filestore.OpenIndex(filestore.IndexDir(p.Getenv, p.Home))
	if err != nil {
		return fmt.Errorf("opening the task index: %w", err)
	}
	clk := clock.System{}
	return daemon.Run(ctx, daemon.Config{
		Dir:     dir,
		Version: p.Version,
		Clock:   clk,
		Ready:   ready,
		Logf: func(format string, args ...any) {
			fmt.Fprintf(os.Stderr, "%s vibesys-cli daemon: %s\n", clk.Now().UTC().Format("2006-01-02T15:04:05Z"), fmt.Sprintf(format, args...))
		},
		Runtime: daemon.Runtime{
			Stores: filestores.New(index, app.Rules(filestores.DefaultSamples)),
			Supervisors: host.New(dir.Supervisors(), func(runDir string) *exec.Cmd {
				return exec.Command(p.Executable, "supervisor", "serve", "--run-dir", runDir)
			}, p.Version),
			Environments: environment.None{},
		},
	})
}

// RunSupervisor is a supervisor process: `vibesys-cli supervisor serve`. ready is the readiness
// pipe the daemon passed.
func (p Process) RunSupervisor(ctx context.Context, runDir string, ready io.WriteCloser) error {
	if !filepath.IsAbs(runDir) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("--run-dir: %q is not an absolute path", runDir)}
	}
	return host.Serve(ctx, runDir, host.Deps{
		Environments:  environment.None{},
		Checkouts:     noCheckouts{},
		BinaryVersion: p.Version,
	}, ready)
}

// ErrAlreadyRunning is what RunDaemon returns when another daemon of the user runs.
var ErrAlreadyRunning = daemon.ErrAlreadyRunning

// ReadyFD is the descriptor the readiness pipe of both processes is on.
const ReadyFD = runtimedir.ReadyFD

// noCheckouts is the Checkouts of a build that cannot check out a commit yet.
type noCheckouts struct{}

var _ supervisor.Checkouts = noCheckouts{}

func (noCheckouts) Checkout(context.Context, string, core.Commit) (core.Checkout, func(), error) {
	return core.Checkout{}, nil, &core.Failure{Kind: core.FailureUnavailable, Message: "this build cannot check out a commit to run a job in"}
}
