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
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	hostenv "github.com/uw-syfi/vibesys/cli/internal/environment/host"
	"github.com/uw-syfi/vibesys/cli/internal/gitwork"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/host"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Process is what the binary knows of itself and of the process it runs in.
type Process struct {
	// Version is the release version of the build.
	Version string
	// Executable is the path of the running binary, which starts the daemon and the supervisors.
	Executable string
	Getenv     func(string) string
	// UID and GID are the user and group the process runs as. Docker jobs run as them, so the
	// files a job writes are the user's.
	UID  int
	GID  int
	Home string
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
		Dir: dir, Version: p.Version,
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
	environments, err := p.machineEnvironments(clk, dir)
	if err != nil {
		return err
	}
	return daemon.Run(ctx, daemon.Config{
		Dir:     dir,
		Version: p.Version,
		Clock:   clk,
		Ready:   ready,
		Logf: func(format string, args ...any) {
			fmt.Fprintf(os.Stderr, "%s vibesys-cli daemon: %s\n", clk.Now().UTC().Format("2006-01-02T15:04:05Z"), fmt.Sprintf(format, args...))
		},
		Runtime: daemon.Runtime{
			Stores: filestores.New(index, app.Rules(task.DefaultSamples)),
			Supervisors: host.New(dir.Supervisors(), func(runDir string) *exec.Cmd {
				return exec.Command(p.Executable, "supervisor", "serve", "--run-dir", runDir)
			}, p.Version),
			Environments: environments,
			Tasks:        gitwork.Tasks{},
		},
	})
}

// RunSupervisor is a supervisor process: `vibesys-cli supervisor serve`. ready is the readiness
// pipe the daemon passed.
func (p Process) RunSupervisor(ctx context.Context, runDir string, ready io.WriteCloser) error {
	if !filepath.IsAbs(runDir) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("--run-dir: %q is not an absolute path", runDir)}
	}
	dir, err := p.RuntimeDir()
	if err != nil {
		return err
	}
	clk := clock.System{}
	environments, err := p.machineEnvironments(clk, dir)
	if err != nil {
		return err
	}
	return host.Serve(ctx, runDir, host.Deps{
		Environments:  environments,
		Checkouts:     gitwork.Checkouts{},
		Host:          supervisor.Host{Backend: cpu.New(cpu.Host(), clk), Files: hostfs.OS{}, Clock: clk},
		BinaryVersion: p.Version,
	}, ready)
}

// machineEnvironments are the environments this machine has: the host, and docker over the docker
// program on the user's PATH. Both are measured by the CPU backend. The daemon fingerprints them
// and the supervisors run jobs in them, so both see the same.
//
// Making the docker environment does not touch Docker: a machine without it fails a docker job
// with a message that says so, and host jobs are unaffected. The containers of the jobs of one
// daemon are one scope, named by its runtime directory, because the daemon runs one job at a time.
func (p Process) machineEnvironments(clk clock.System, dir runtimedir.Dir) (environment.Environments, error) {
	machine := cpu.New(cpu.Host(), clk)
	engine, err := dockercli.New(dockercli.Config{Timers: clk})
	if err != nil {
		return nil, err
	}
	containers, err := docker.New(docker.Config{
		Engine: engine, Source: docker.TaskSource{Tasks: gitwork.Tasks{}}, Backend: machine, Timers: clk,
		IDs: dockercli.RandomIDs{}, Scope: docker.ScopeFor(dir.Path()), User: docker.User{UID: p.UID, GID: p.GID},
	})
	if err != nil {
		return nil, err
	}
	onHost := hostenv.New(hostenv.Config{Backend: machine, Timers: clk})
	return environment.Kinds{
		core.EnvironmentHost:   func(environment.Target) (environment.Environment, error) { return onHost, nil },
		core.EnvironmentDocker: containers.Environment,
	}, nil
}

// ErrAlreadyRunning is what RunDaemon returns when another daemon of the user runs.
var ErrAlreadyRunning = daemon.ErrAlreadyRunning

// ReadyFD is the descriptor the readiness pipe of both processes is on.
const ReadyFD = runtimedir.ReadyFD
