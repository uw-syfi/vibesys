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
	// MachineDir is the user's machine directory, where the one daemon of the user on this machine
	// holds its lock. Empty is runtimedir.ResolveMachine's, which no variable moves; a test of two
	// daemons sets a directory of its own.
	MachineDir string
}

// RuntimePath is the runtime directory of this process: the one it was told to use (JobToolPlace), and
// otherwise the user's, which runtimedir resolves.
func (p Process) RuntimePath() (string, error) {
	chosen, err := p.chosenRuntime()
	if err != nil || chosen != "" {
		return chosen, err
	}
	return runtimedir.Resolve(p.Getenv, p.UID), nil
}

// RuntimeDir opens the runtime directory of this process.
func (p Process) RuntimeDir() (runtimedir.Dir, error) {
	path, err := p.RuntimePath()
	if err != nil {
		return runtimedir.Dir{}, err
	}
	return runtimedir.Open(path, p.UID, runtimedir.System())
}

const daemonName = "vibesysd"

// ClientOptions returns how a client reaches the daemon, starting it from this binary when none
// runs.
func (p Process) ClientOptions() (client.Options, error) {
	// A sandbox's commands reach the sandbox daemon only: one set to reach the daemon that holds
	// the machine is refused here, before it does.
	if _, err := p.sandbox(); err != nil {
		return client.Options{}, err
	}
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
		SetUp: func() error {
			_, err := p.recordPlaces(dir)
			return err
		},
	}, nil
}

// RunDaemon is the daemon process: `vibesys-cli daemon run`. ready is the readiness pipe a
// client that started the daemon passed.
func (p Process) RunDaemon(ctx context.Context, ready io.WriteCloser) error {
	dir, err := p.RuntimeDir()
	if err != nil {
		return err
	}
	places, err := p.daemonPlaces(dir)
	if err != nil {
		return err
	}
	indexDir := filestore.IndexDirIn(places.GetStateHome())
	index, err := filestore.OpenIndex(indexDir)
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
	// The daemon runs no container: it only fingerprints the environments.
	machines, err := p.machineEnvironments(op, clk, dir, dir.Path())
	if err != nil {
		return err
	}
	scheduler, err := schedulerOf(op, machines.offers, machines.claims)
	if err != nil {
		return err
	}
	logf := func(format string, args ...any) {
		fmt.Fprintf(os.Stderr, "%s %s: %s\n", clk.Now().UTC().Format("2006-01-02T15:04:05Z"), daemonName, fmt.Sprintf(format, args...))
	}
	if here := filestore.StateHome(p.Getenv, p.Home); here != places.GetStateHome() {
		logf("this daemon keeps its state in %s, as %s records; this environment alone would say %s", places.GetStateHome(), dir.Path(), here)
	}
	allocations, err := allocationsOf(op, indexDir, machines.clusters)
	if err != nil {
		return err
	}
	hold, err := p.daemonMachine(dir, clk)
	if err != nil {
		return err
	}
	scope, err := leaseScopeOf(hold.Sandbox(), dir, func() (string, error) { return runtimedir.ResolveLeases(runtimedir.System()) })
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
		Machine:   hold,
		Logf:      logf,
		Runtime: daemon.Runtime{
			Stores: filestores.New(index, app.Rules(task.DefaultSamples), owner),
			Supervisors: host.New(host.Config{
				Root:    dir.Supervisors(),
				Records: host.RecordsDir(indexDir, machine, dir.Path()),
				BootID:  runtimedir.BootID,
				Command: func(runDir string) *exec.Cmd {
					return exec.Command(p.Executable, "supervisor", "serve", "--run-dir", runDir)
				},
				BinaryVersion: p.Version,
				Timers:        clock.System{},
			}),
			Environments:    machines.environments,
			Placements:      machines.offers,
			PlacementReader: app.NewPlacementReader(machines.probes, clk, clk, app.DefaultCallTimeout),
			Branches:        gitrepo.System{},
			Repositories:    gitrepo.System{},
			Tasks:           gitwork.Tasks{},
			CardFiles:       gitwork.CardFiles{},
			Scheduler:       scheduler,
			Leasing:         p.leasingOf(op, scope, machines.claims, clk, logf),
			Allocations:     allocations,
			Capacities:      capacitiesOf(machines.clusters),
			Neighbors:       neighborsOf,
		},
	})
}

// policies are the scheduling policies, by the name the operator config gives them. This is the
// only place that names them.
var policies = map[string]func() schedule.Policy{
	fifo.Name:      fifo.New,
	fairshare.Name: fairshare.New,
}

// hostResource is the resource host jobs claim when no local placement is the default one.
const hostResource schedule.ResourceID = "host"

// schedulerOf is how the operator config shares the machines: the named policy, a demand that lets
// a docker job run on every placement that gives what its task needs (offers says what each gives),
// a resource of max_jobs slots for each placement, and one for each resource the placements'
// resources claim, with room for every job of the placements that claim it, so that it bounds only
// the claims that take it whole: a measure's, but on a placement that keeps it apart by itself
// (isolated), and those a resource makes exclusive, such as a cluster's. A host job runs on this
// machine beside the jobs of the local placement "default" of the earlier shape, so it claims a
// slot of that placement; without one, host jobs have a resource of one slot of their own.
func schedulerOf(op *cliv1.OperatorConfig, offers []core.PlacementOffer, claims map[string][]core.Claim) (app.Scheduler, error) {
	policy := op.GetScheduler().GetPolicy()
	newPolicy, ok := policies[policy]
	if !ok {
		names := slices.Sorted(maps.Keys(policies))
		return app.Scheduler{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"the operator config: scheduler.policy: %q is not a policy (%s)", policy, strings.Join(names, ", "))}
	}
	demand := schedule.PlacementDemand{Placements: map[string]schedule.Offered{}}
	var resources []schedule.Resource
	shared := map[schedule.ResourceID]int{}
	for _, name := range slices.Sorted(maps.Keys(op.GetPlacements())) {
		slots := int(op.GetPlacements()[name].GetMaxJobs())
		resources = append(resources, schedule.Resource{ID: schedule.PlacementOf(name), Capacity: slots})
		offered := schedule.Offered{Isolated: isolated(op.GetPlacements()[name])}
		if i := slices.IndexFunc(offers, func(o core.PlacementOffer) bool { return o.Name == name }); i >= 0 {
			offered.Resources = offers[i].Resources
		}
		for _, c := range claims[name] {
			id := schedule.ResourceID(c.Resource)
			offered.Claims = append(offered.Claims, schedule.Claim{Resource: id, Amount: c.Amount, Exclusive: c.Exclusive})
			shared[id] += slots * max(c.Amount, 1)
		}
		demand.Placements[name] = offered
	}
	for _, id := range slices.Sorted(maps.Keys(shared)) {
		resources = append(resources, schedule.Resource{ID: id, Capacity: shared[id]})
	}
	demand.Host = hostResource
	if d := op.GetPlacements()[operator.DefaultPlacement]; d.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_LOCAL {
		demand.Host = schedule.PlacementOf(operator.DefaultPlacement)
	} else {
		resources = append(resources, schedule.Resource{ID: hostResource, Capacity: 1})
	}
	return app.Scheduler{Resources: resources, Policy: newPolicy(), Demand: demand}, nil
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
