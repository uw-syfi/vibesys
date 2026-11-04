package production

import (
	"context"
	"fmt"
	"io"
	"maps"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/filestores"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/backend/nvidia"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/machine"
	hostenv "github.com/uw-syfi/vibesys/cli/internal/environment/host"
	"github.com/uw-syfi/vibesys/cli/internal/environment/slurm"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/gitwork"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fairshare"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
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
	// Environ returns the whole environment, which the daemon's jobs inherit.
	Environ func() []string
	// UID and GID are the user and group the process runs as. Docker jobs run as them, so the
	// files a job writes are the user's.
	UID  int
	GID  int
	Home string
}

// RuntimePath is where the user's runtime directory is: under XDG_RUNTIME_DIR when it is an
// absolute path, and otherwise a private directory under /tmp.
func (p Process) RuntimePath() string { return runtimedir.Resolve(p.Getenv, p.UID) }

// RuntimeDir opens the user's runtime directory.
func (p Process) RuntimeDir() (runtimedir.Dir, error) {
	return runtimedir.Open(p.RuntimePath(), p.UID, runtimedir.System())
}

// IndexDir is where the task index is, which filestore decides from the environment and the home.
func (p Process) IndexDir() string { return filestore.IndexDir(p.Getenv, p.Home) }

const daemonName = "vibesysd"

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
			cmd := exec.Command(p.Executable, "daemon", "run")
			cmd.Args[0] = daemonName
			return cmd
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
	index, err := filestore.OpenIndex(p.IndexDir())
	if err != nil {
		return fmt.Errorf("opening the task index: %w", err)
	}
	machine, err := os.Hostname()
	if err != nil {
		return fmt.Errorf("naming this machine as the owner of its stores: %w", err)
	}
	owner := filestore.Owner{Host: machine, Dir: dir.Path(), PID: os.Getpid()}
	clk := clock.System{}
	op, err := p.operator()
	if err != nil {
		return err
	}
	scheduler, err := schedulerOf(op.Scheduler)
	if err != nil {
		return err
	}
	// The daemon runs no container: it only fingerprints the environments.
	environments, err := p.machineEnvironments(op, clk, dir, dir.Path())
	if err != nil {
		return err
	}
	return daemon.Run(ctx, daemon.Config{
		Dir:     dir,
		Version: p.Version,
		// The supervisors are started with the daemon's environment and make the checkouts, so
		// they make them where the daemon would.
		Inherited: daemon.Inherited{Environ: p.Environ(), CheckoutDirectory: gitwork.Checkouts{}.Dir()},
		Clock:     clk,
		Ready:     ready,
		Logf: func(format string, args ...any) {
			fmt.Fprintf(os.Stderr, "%s %s: %s\n", clk.Now().UTC().Format("2006-01-02T15:04:05Z"), daemonName, fmt.Sprintf(format, args...))
		},
		Runtime: daemon.Runtime{
			Stores: filestores.New(index, app.Rules(task.DefaultSamples), owner),
			Supervisors: host.New(host.Config{
				Root:    dir.Supervisors(),
				Records: host.RecordsDir(p.IndexDir(), machine, dir.Path()),
				BootID:  runtimedir.BootID,
				Command: func(runDir string) *exec.Cmd {
					return exec.Command(p.Executable, "supervisor", "serve", "--run-dir", runDir)
				},
				BinaryVersion: p.Version,
				Timers:        clock.System{},
			}),
			Environments: environments,
			Branches:     gitrepo.System{},
			Repositories: gitrepo.System{},
			Tasks:        gitwork.Tasks{},
			Scheduler:    scheduler,
		},
	})
}

// policies are the scheduling policies, by the name the operator config gives them. This is the
// only place that names them.
var policies = map[string]func() schedule.Policy{
	fifo.Name:      fifo.New,
	fairshare.Name: fairshare.New,
}

// schedulerOf is how the operator config shares the machine: max_jobs slots of the one placement,
// which a measure job takes alone, and the named policy.
func schedulerOf(s operator.Scheduler) (app.Scheduler, error) {
	newPolicy, ok := policies[s.Policy]
	if !ok {
		names := slices.Sorted(maps.Keys(policies))
		return app.Scheduler{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"the operator config: scheduler.policy: %q is not a policy (%s)", s.Policy, strings.Join(names, ", "))}
	}
	return app.Scheduler{
		Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: s.MaxJobs}},
		Policy:    newPolicy(),
		Demand:    schedule.DefaultDemand{},
	}, nil
}

func (p Process) operator() (operator.Config, error) {
	return operator.Load(operator.Path(p.Getenv, p.Home), p.Getenv(operator.EnvConfig) != "")
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
	op, err := p.operator()
	if err != nil {
		return err
	}
	// The containers of this run are a scope of their own: the daemon may run other jobs beside it,
	// and a run removes the containers of its scope that it does not own.
	environments, err := p.machineEnvironments(op, clk, dir, runDir)
	if err != nil {
		return err
	}
	return host.Serve(ctx, runDir, host.Deps{
		Environments:  environments,
		Checkouts:     gitwork.Checkouts{},
		Host:          supervisor.Host{Files: hostfs.OS{}, Clock: clk, Artifacts: filestore.ArtifactsDir},
		BinaryVersion: p.Version,
	}, ready)
}

// machineEnvironments are the environments this machine has: the host, and docker, whose engine
// the operator config places on this machine, on another one over ssh, or on another one inside
// Slurm allocations. The daemon fingerprints them and the supervisors run jobs in them, so both see
// the same.
//
// Making the docker environment does not touch Docker or another machine: a machine without it
// fails a docker job with a message that says so, and host jobs are unaffected. A config that cannot
// be read stops the daemon, since a job run without what the operator asked for would mislead.
//
// What one daemon stages and builds is one scope, named by its runtime directory. On this machine
// the containers of one run are a scope of their own, named by containerKey, the run's directory,
// because the daemon may run several jobs at once there: a run sweeps away the containers of its
// scope it does not own, which are what an earlier attempt of the same job left. Another machine
// runs one job of the daemon at a time (operator.Parse refuses more), so there the containers and
// the Slurm allocations stay in the daemon's scope, which a run sweeps whole.
func (p Process) machineEnvironments(op operator.Config, clk clock.System, dir runtimedir.Dir, containerKey string) (environment.Environments, error) {
	scope := docker.ScopeFor(dir.Path())
	local := cpu.New(cpu.Host(), clk)
	cfg := docker.Config{
		Source: docker.TaskSource{Tasks: gitwork.Tasks{}}, Timers: clk, IDs: dockercli.RandomIDs{}, Scope: scope,
		User: docker.User{UID: p.UID, GID: p.GID}, GPUs: docker.GPUs{Devices: op.Docker.GPUs, Runtime: op.Docker.Runtime},
		Mounts: op.Docker.Mounts,
	}
	engineCfg := dockercli.Config{Timers: clk}
	switch op.Docker.Placement {
	case operator.Local:
		cfg.Scope = docker.ScopeFor(containerKey)
		cfg.Backend = local
		cfg.Machine = machine.Local{CacheRoot: p.cacheRoot(), As: cfg.User}
		cfg.GPUFacts = &nvidia.GPUs{Shell: remote.Local{}, Timers: clk}
	default:
		r := op.Remote
		ssh := remote.SSH{Host: r.Host, Program: r.SSHCommand}
		engineCfg.Shell = ssh
		cfg.Backend = cpu.New(&remote.CPU{Shell: ssh, Timers: clk, Timeout: machine.CallTimeout}, clk)
		cfg.Machine = &machine.Remote{Shell: ssh, Rsync: remote.Rsync{SSH: ssh, Program: r.RsyncCommand}, Root: r.WorkspaceRoot, Scope: scope, Timers: clk}
		cfg.GPUFacts = &nvidia.GPUs{Shell: ssh, Timers: clk}
		if s := op.Slurm; s != nil {
			engineCfg.Allocator = slurm.Allocator{
				Partition: s.Partition, Gres: s.Gres, SharedGres: s.SharedGres, Exclusive: s.Exclusive, CPUs: s.CPUs, Memory: s.Memory,
				Arguments: s.SrunArguments, Builder: slurm.BuilderName(scope),
			}
			engineCfg.StartTimeout = s.QueueTimeout
		}
	}
	engine, err := dockercli.New(engineCfg)
	if err != nil {
		return nil, err
	}
	cfg.Engine = engine
	if s := op.Slurm; s != nil {
		cfg.Engine = slurm.Engine{
			Inner: engine, Scheduler: slurm.CLI{Shell: engineCfg.Shell, Timers: clk, Poll: s.PollInterval},
			Builders: slurm.Buildx{Shell: engineCfg.Shell, Timers: clk, Name: slurm.BuilderName(scope)},
		}
	}
	containers, err := docker.New(cfg)
	if err != nil {
		return nil, err
	}
	onHost := hostenv.New(hostenv.Config{Backend: local, Timers: clk})
	return environment.Kinds{
		core.EnvironmentHost:   func(environment.Target) (environment.Environment, error) { return onHost, nil },
		core.EnvironmentDocker: containers.Environment,
	}, nil
}

// cacheRoot is where docker jobs on this machine keep their caches: vibesys-cli in XDG_CACHE_HOME,
// or in ~/.cache when that is not an absolute path.
func (p Process) cacheRoot() string {
	if dir := p.Getenv("XDG_CACHE_HOME"); filepath.IsAbs(dir) {
		return filepath.Join(dir, "vibesys-cli")
	}
	return filepath.Join(p.Home, ".cache", "vibesys-cli")
}

// ErrAlreadyRunning is what RunDaemon returns when another daemon of the user runs.
var ErrAlreadyRunning = daemon.ErrAlreadyRunning

// ReadyFD is the descriptor the readiness pipe of both processes is on.
const ReadyFD = runtimedir.ReadyFD
