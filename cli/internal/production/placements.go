package production

import (
	"fmt"
	"maps"
	"slices"
	"strings"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/backend/nvidia"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/machine"
	hostenv "github.com/uw-syfi/vibesys/cli/internal/environment/host"
	"github.com/uw-syfi/vibesys/cli/internal/environment/slurm"
	"github.com/uw-syfi/vibesys/cli/internal/gitwork"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/gpu"
	"github.com/uw-syfi/vibesys/cli/internal/resource/kubernetes"
	"github.com/uw-syfi/vibesys/cli/internal/resource/mounts"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
)

// machines are the environments this machine's jobs run in, and the placements a docker job can
// run on.
type machines struct {
	environments environment.Environments
	offers       []core.PlacementOffer
	// claims are what the resources of each placement claim for a job that runs on it.
	claims map[string][]core.Claim
}

// machineEnvironments are the environments this machine has: the host, and docker on each
// placement of the operator config, whose engine runs on this machine, on another one over ssh,
// or on another one inside Slurm allocations, with the resources the placement gives. The daemon
// fingerprints them and the supervisors run jobs in them, so both see the same.
//
// Making them does not touch Docker or another machine: a machine without it fails a docker job
// with a message that says so, and host jobs are unaffected. A config that cannot be read stops
// the daemon, since a job run without what the operator asked for would mislead.
//
// What one daemon stages and builds is one scope, named by its runtime directory. On this machine
// the containers of one run are a scope of their own, named by containerKey, the run's directory,
// because the daemon may run several jobs at once there: a run sweeps away the containers of its
// scope it does not own, which are what an earlier attempt of the same job left. Another machine
// runs one job of the daemon at a time (operator.Parse refuses more), so there the containers and
// the Slurm allocations stay in the daemon's scope, which a run sweeps whole.
func (p Process) machineEnvironments(op *cliv1.OperatorConfig, clk clock.System, dir runtimedir.Dir, containerKey string) (machines, error) {
	local := cpu.New(cpu.Host(), clk)
	providers := map[string]*docker.Provider{}
	out := machines{claims: map[string][]core.Claim{}}
	names := slices.Sorted(maps.Keys(op.GetPlacements()))
	// Zero is the default grace: the environments take it from environment.DefaultGrace.
	grace := seconds(op.GetStopGraceSeconds(), 0)
	for _, name := range names {
		provider, err := p.placement(clk, dir, containerKey, local, name, op.GetPlacements()[name], grace)
		if err != nil {
			return machines{}, fmt.Errorf("the operator config %s, placements.%s: %w", operator.Path(p.Getenv, p.Home), name, err)
		}
		providers[name] = provider
		out.offers = append(out.offers, provider.Offer())
		out.claims[name] = provider.Claims()
	}
	onHost := hostenv.New(hostenv.Config{Backend: local, Timers: clk, Grace: grace})
	out.environments = environment.Kinds{
		core.EnvironmentHost: func(environment.Target) (environment.Environment, error) { return onHost, nil },
		core.EnvironmentDocker: func(t environment.Target) (environment.Environment, error) {
			provider, ok := providers[t.Placement]
			if !ok {
				return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
					"placement %q is not in the operator config (its placements: %s)", t.Placement, strings.Join(names, ", "))}
			}
			return provider.Environment(t)
		},
	}
	return out, nil
}

// scopeOf is the scope of a placement's work, keyed by a directory: the daemon's runtime directory,
// or a run's. The placement of a file of the earlier shape keeps the scope the daemon's one
// placement had, so a daemon of this build sweeps what one before it left; another placement has one
// of its own, so that two placements on one engine never sweep each other's containers.
func scopeOf(key, name string) string {
	if name == operator.DefaultPlacement {
		return docker.ScopeFor(key)
	}
	return docker.ScopeFor(key + "\x00placement\x00" + name)
}

// placement makes the docker environments of one placement.
func (p Process) placement(clk clock.System, dir runtimedir.Dir, containerKey string, local *cpu.Backend, name string, pl *cliv1.Placement, grace time.Duration) (*docker.Provider, error) {
	scope := scopeOf(dir.Path(), name)
	cfg := docker.Config{
		Source: docker.TaskSource{Tasks: gitwork.Tasks{}}, Timers: clk, IDs: dockercli.RandomIDs{}, Scope: scope,
		User: docker.User{UID: p.UID, GID: p.GID}, Placement: name, Grace: grace,
	}
	engineCfg := dockercli.Config{Timers: clk}
	var shell remote.Shell = remote.Local{}
	where := "local"
	if pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_LOCAL {
		cfg.Scope = scopeOf(containerKey, name)
		cfg.Backend = local
		cfg.Machine = machine.Local{CacheRoot: p.cacheRoot(), As: cfg.User}
	} else {
		r := pl.GetRemote()
		ssh := remote.SSH{Host: r.GetHost(), Program: orDefault(r.GetSshCommand(), "ssh")}
		shell, where, engineCfg.Shell = ssh, r.GetHost(), ssh
		cfg.Backend = cpu.New(&remote.CPU{Shell: ssh, Timers: clk, Timeout: machine.CallTimeout}, clk)
		cfg.Machine = &machine.Remote{
			Shell: ssh, Rsync: remote.Rsync{SSH: ssh, Program: orDefault(r.GetRsyncCommand(), "rsync")},
			Root: r.GetRemoteWorkspaceRoot(), Scope: scope, Timers: clk,
		}
	}
	s := pl.GetSlurm()
	if s != nil {
		exclusive := true
		if s.Exclusive != nil {
			exclusive = s.GetExclusive()
		}
		engineCfg.Allocator = slurm.Allocator{
			Partition: s.GetPartition(), Gres: s.GetGres(), SharedGres: s.GetSharedGres(), Exclusive: exclusive, CPUs: int(s.GetCpus()),
			Memory: s.GetMemory(), Arguments: s.GetSrunArguments(), Builder: slurm.BuilderName(scope),
		}
		engineCfg.StartTimeout = seconds(s.GetQueueTimeoutSeconds(), slurm.DefaultQueueTimeout)
	}
	engine, err := dockercli.New(engineCfg)
	if err != nil {
		return nil, err
	}
	cfg.Engine = engine
	if s != nil {
		cfg.Engine = slurm.Engine{
			Inner: engine, Scheduler: slurm.CLI{Shell: engineCfg.Shell, Timers: clk, Poll: seconds(s.GetPollIntervalSeconds(), slurm.DefaultPollInterval)},
			Builders: slurm.Buildx{Shell: engineCfg.Shell, Timers: clk, Name: slurm.BuilderName(scope)},
		}
	}
	if cfg.Resources, err = resources(pl.GetResources(), shell, where, clk); err != nil {
		return nil, err
	}
	return docker.New(cfg)
}

// resources are the resources of a placement, each made by its own package from its section.
func resources(r *cliv1.Resources, shell remote.Shell, where string, clk clock.System) ([]resource.Resource, error) {
	var out []resource.Resource
	if g := r.GetGpu(); g != nil {
		res, err := gpu.New(gpu.Config{Devices: g.GetDevices(), Runtime: g.GetRuntime(), Machine: where, Facts: &nvidia.GPUs{Shell: shell, Timers: clk}})
		if err != nil {
			return nil, err
		}
		out = append(out, res)
	}
	if k := r.GetKubernetes(); k != nil {
		res, err := kubernetes.New(kubernetes.Config{
			Kubeconfig: k.GetKubeconfig(), Registry: k.GetRegistry(), Builder: k.GetBuilder(), Network: k.GetNetwork(),
			Machine: where, Cluster: kubernetes.Kubectl{Shell: shell, Timers: clk},
		})
		if err != nil {
			return nil, err
		}
		out = append(out, res)
	}
	if m := r.GetMounts(); len(m) > 0 {
		res, err := mounts.New(m)
		if err != nil {
			return nil, err
		}
		out = append(out, res)
	}
	return out, nil
}

// orDefault is a program the config names, or the default one when it names none.
func orDefault(argv []string, program string) []string {
	if len(argv) == 0 {
		return []string{program}
	}
	return argv
}

// seconds is a wait the config gives in seconds, or the default when it gives none.
func seconds(s float64, otherwise time.Duration) time.Duration {
	if s > 0 {
		return time.Duration(s * float64(time.Second))
	}
	return otherwise
}
