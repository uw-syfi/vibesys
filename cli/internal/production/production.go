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

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/filestores"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/gitwork"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
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
	scheduler, err := schedulerOf(op)
	if err != nil {
		return err
	}
	// The daemon runs no container: it only fingerprints the environments.
	machines, err := p.machineEnvironments(op, clk, dir, dir.Path())
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
			Environments: machines.environments,
			Placements:   machines.offers,
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

// schedulerOf is how the operator config shares the machine: the named policy, and slots of the
// one placement resource DefaultDemand claims, which a measure job takes alone. A config of one
// placement gives it that placement's max_jobs. A config of several gives it one, so their jobs run
// one at a time between them, until the demand claims the placement each job runs on.
func schedulerOf(op *cliv1.OperatorConfig) (app.Scheduler, error) {
	policy := op.GetScheduler().GetPolicy()
	newPolicy, ok := policies[policy]
	if !ok {
		names := slices.Sorted(maps.Keys(policies))
		return app.Scheduler{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"the operator config: scheduler.policy: %q is not a policy (%s)", policy, strings.Join(names, ", "))}
	}
	slots := 1
	if placements := op.GetPlacements(); len(placements) == 1 {
		for _, pl := range placements {
			slots = int(pl.GetMaxJobs())
		}
	}
	return app.Scheduler{
		Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: slots}},
		Policy:    newPolicy(),
		Demand:    schedule.DefaultDemand{},
	}, nil
}

func (p Process) operator() (*cliv1.OperatorConfig, error) {
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
	machines, err := p.machineEnvironments(op, clk, dir, runDir)
	if err != nil {
		return err
	}
	return host.Serve(ctx, runDir, host.Deps{
		Environments:  machines.environments,
		Checkouts:     gitwork.Checkouts{},
		Host:          supervisor.Host{Files: hostfs.OS{}, Clock: clk, Artifacts: filestore.ArtifactsDir},
		BinaryVersion: p.Version,
	}, ready)
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
