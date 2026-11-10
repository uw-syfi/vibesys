package production

import (
	"fmt"
	"maps"
	"path"
	"runtime"
	"runtime/debug"
	"slices"
	"strings"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/backend/amd"
	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/backend/nvidia"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clustercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/localsif"
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
	"github.com/uw-syfi/vibesys/cli/internal/resource/rocm"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// machines are the environments this machine's jobs run in, and the placements a docker job can
// run on.
type machines struct {
	environments environment.Environments
	offers       []core.PlacementOffer
	// claims are what the resources of each placement claim for a job that runs on it.
	claims map[string][]core.Claim
	probes map[string]app.Placement
	// clusters are what the apptainer placements ask their cluster, by placement: the daemon makes
	// their standing allocators and capacity readers from them.
	clusters map[string]clusterSide
}

// clusterSide is what an apptainer placement's cluster is asked through, beside its engine: the
// cluster, the scope its allocations are found by, and what each is sized for.
type clusterSide struct {
	Cluster   apptainer.Cluster
	Scope     string
	Sizing    apptainer.Sizing
	MaxTime   time.Duration
	Partition string
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
// What one daemon stages and builds is one scope per placement, named by its runtime directory. On
// this machine the containers of one run are a scope of their own, named by containerKey, the run's
// directory, because the daemon may run several jobs at once there: a run sweeps away the
// containers of its scope it does not own, which are what an earlier attempt of the same job left.
// An apptainer placement may run several jobs at once too, each in an allocation of its own, so
// each run is a scope of its own there as well, under the placement's: its staged directories and
// its allocations are named by both, and runs (in the runtime directory) says which runs live, so
// that a run clears what a dead job of the placement left and never what a live one uses. Another
// machine runs one job of the daemon at a time (operator.Parse refuses more), so there the
// containers and the Slurm allocations stay in the placement's scope, which a run sweeps whole.
func (p Process) machineEnvironments(op *cliv1.OperatorConfig, clk clock.System, dir runtimedir.Dir, containerKey string) (machines, error) {
	local := cpu.New(cpu.Host(), clk)
	// One per process, shared by its placements' engines and machines: a second one would wait on
	// the lock the first holds for the same run.
	runs := runtimedir.NewRuns(dir.PlacementRuns())
	// One lock of the builds of each tag per Docker the placements build with, so that jobs of two
	// placements that need one image build it once (engineBuilds).
	builds := engineBuilds{}
	providers := map[string]*docker.Provider{}
	out := machines{claims: map[string][]core.Claim{}, probes: map[string]app.Placement{}, clusters: map[string]clusterSide{}}
	names := slices.Sorted(maps.Keys(op.GetPlacements()))
	// Zero is the default grace: the environments take it from environment.DefaultGrace.
	grace := seconds(op.GetStopGraceSeconds(), 0)
	for _, name := range names {
		provider, side, err := p.placement(clk, dir, containerKey, runs, builds, local, name, op.GetPlacements()[name], grace)
		if err != nil {
			return machines{}, fmt.Errorf("the operator config %s, placements.%s: %w", operator.Path(p.Getenv, p.Home), name, err)
		}
		providers[name] = provider
		if side != nil {
			out.clusters[name] = *side
		}
		probe, err := placementProbe(op.GetPlacements()[name], provider)
		if err != nil {
			return machines{}, fmt.Errorf("the operator config %s, placements.%s: %w", operator.Path(p.Getenv, p.Home), name, err)
		}
		out.probes[name] = probe
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

// probe is the program a local placement's containers probe a candidate's server with: this
// binary, when it is a static build for Linux, as a release is, since the containers run it
// whatever libraries their image has. Any other build has none, and its placements run no task
// of two images.
func (p Process) probe() string {
	if runtime.GOOS != "linux" || !path.IsAbs(p.Executable) || !static() {
		return ""
	}
	return p.Executable
}

// static reports whether this binary was built without cgo, so that it links no C library.
func static() bool {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return false
	}
	for _, s := range info.Settings {
		if s.Key == "CGO_ENABLED" {
			return s.Value == "0"
		}
	}
	return false
}

// TaskSchemas names the task schema versions this build reads, as --version prints them.
func TaskSchemas() string { return task.SchemasText() }

// CurrentVersions are the revisions of the contract this build speaks, as `--version --json` prints
// them beside the build's identity, so a program that would use this tool learns whether it can
// without starting a daemon.
func CurrentVersions() core.Versions { return core.CurrentVersions() }

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

// engineBuilds are the locks of the builds of each tag, by the Docker a placement builds with: this
// machine's ("" names it) for a local placement and for every apptainer placement, whose images
// this machine's Docker builds, and the remote host's for a remote placement.
type engineBuilds map[string]*docker.Builds

// of returns the lock of the builds on the Docker of the host, made on first use.
func (e engineBuilds) of(host string) *docker.Builds {
	b, ok := e[host]
	if !ok {
		b = docker.NewBuilds()
		e[host] = b
	}
	return b
}

// placement makes the docker environments of one placement.
func (p Process) placement(clk clock.System, dir runtimedir.Dir, containerKey string, runs docker.Runs, builds engineBuilds, local *cpu.Backend, name string, pl *cliv1.Placement, grace time.Duration) (*docker.Provider, *clusterSide, error) {
	scope := scopeOf(dir.Path(), name)
	cfg := docker.Config{
		Source: docker.TaskSource{Tasks: gitwork.Tasks{}}, Timers: clk, IDs: dockercli.RandomIDs{}, Scope: scope,
		User: docker.User{UID: p.UID, GID: p.GID}, Placement: name, Grace: grace,
		Candidates: docker.CandidateFiles{Files: gitwork.Tasks{}},
	}
	engineCfg := dockercli.Config{Timers: clk}
	var shell remote.Shell = remote.Local{}
	where := "local"
	if pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_LOCAL {
		cfg.Scope = scopeOf(containerKey, name)
		cfg.Backend = local
		cfg.Machine = machine.Local{CacheRoot: p.cacheRoot(), As: cfg.User}
		cfg.Probe = p.probe()
		cfg.Builds = builds.of("")
	} else {
		r := pl.GetRemote()
		ssh := remote.SSH{Host: r.GetHost(), Program: orDefault(r.GetSshCommand(), "ssh")}
		shell, where, engineCfg.Shell = ssh, r.GetHost(), ssh
		cfg.Backend = cpu.New(&remote.CPU{Shell: ssh, Timers: clk, Timeout: machine.CallTimeout}, clk)
		staging := &machine.Remote{
			Shell: ssh, Rsync: remote.Rsync{SSH: ssh, Program: orDefault(r.GetRsyncCommand(), "rsync"), Timers: clk},
			Root: r.GetRemoteWorkspaceRoot(), Scope: scope, Timers: clk,
		}
		cfg.Builds = builds.of(r.GetHost())
		if pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER {
			// Each job's runs are a scope of their own, under the placement's.
			cfg.Scope = scopeOf(containerKey, name)
			staging.Run, staging.Runs = cfg.Scope, runs
			// Its images are built by this machine's Docker.
			cfg.Builds = builds.of("")
		}
		cfg.Machine = staging
	}
	s := pl.GetSlurm()
	if s != nil {
		engineCfg.Allocator = slurm.Allocator{
			Partition: s.GetPartition(), Gres: s.GetGres(), SharedGres: s.GetSharedGres(), Exclusive: exclusiveOf(s), CPUs: int(s.GetCpus()),
			Memory: s.GetMemory(), Arguments: s.GetSrunArguments(), Builder: slurm.BuilderName(scope),
		}
		engineCfg.StartTimeout = seconds(s.GetQueueTimeoutSeconds(), slurm.DefaultQueueTimeout)
	}
	if pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER {
		return p.apptainerPlacement(clk, cfg, scope, runs, pl)
	}
	engine, err := dockercli.New(engineCfg)
	if err != nil {
		return nil, nil, err
	}
	cfg.Engine = engine
	if s != nil {
		cfg.Engine = slurm.Engine{
			Inner: engine, Scheduler: slurm.CLI{Shell: engineCfg.Shell, Timers: clk, Poll: seconds(s.GetPollIntervalSeconds(), slurm.DefaultPollInterval)},
			Builders: slurm.Buildx{Shell: engineCfg.Shell, Timers: clk, Name: slurm.BuilderName(scope)},
		}
	}
	if cfg.Resources, err = resources(pl.GetResources(), shell, where, where, clk, &amd.GPUs{Shell: shell, Timers: clk}); err != nil {
		return nil, nil, err
	}
	provider, err := docker.New(cfg)
	return provider, nil, err
}

// exclusiveOf says whether a measured container's allocation holds its whole node: [slurm]
// exclusive, true by default.
func exclusiveOf(s *cliv1.SlurmSettings) bool {
	return s == nil || s.Exclusive == nil || s.GetExclusive()
}

// isolated says whether the placement keeps a measure apart from its other jobs by itself
// (schedule.Offered.Isolated): an apptainer placement of [slurm] exclusive runs each job in
// allocations of its own, and a measured container's holds its whole node, a reused one included
// (apptainer.Engine sizes every reusable allocation for a measured container, and a claim of one
// is its alone). An ssh or slurm placement runs its jobs' containers in one Docker daemon outside
// Slurm's cgroups, and a local one on this machine, so a measure holds those whole.
func isolated(pl *cliv1.Placement) bool {
	return pl.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER && exclusiveOf(pl.GetSlurm())
}

// gpuScript names the node script whose output the rocm resource of an apptainer placement reads.
const gpuScript = "gpu"

// apptainerPlacement makes the docker environments of a placement of the apptainer kind: this
// machine's Docker builds and exports the images, the login node stages the runs' directories on
// the file system the nodes share, and the cluster runs the containers in its allocations. The
// machine and the GPUs are what the nodes say inside the allocations, never the login node.
//
// cfg.Scope is the run's, and scope the placement's, under which runs says which runs live.
func (p Process) apptainerPlacement(clk clock.System, cfg docker.Config, scope string, runs docker.Runs, pl *cliv1.Placement) (*docker.Provider, *clusterSide, error) {
	local, err := dockercli.New(dockercli.Config{Timers: clk})
	if err != nil {
		return nil, nil, err
	}
	s, a := pl.GetSlurm(), pl.GetApptainer()
	runtime := apptainer.RuntimeApptainer
	var converter apptainer.Converter
	var localDir string
	if a.GetRuntime() == cliv1.ContainerRuntime_CONTAINER_RUNTIME_PYXIS {
		// The login node makes each image's squashfs from its files, streamed from here.
		runtime = apptainer.RuntimePyxis
	} else {
		// The images are converted to SIFs here, where Docker built them; the cluster only runs them.
		sif, err := localsif.New(p.Getenv("PATH"), clk, cfg.Grace)
		if err != nil {
			return nil, nil, err
		}
		converter, localDir = sif, p.localScratch(a)
	}
	ssh := remote.SSH{Host: pl.GetRemote().GetHost(), Program: orDefault(pl.GetRemote().GetSshCommand(), "ssh")}
	cluster, err := clustercli.New(clustercli.Config{
		Shell: ssh, Timers: clk, Submit: orDefault(a.GetSubmitCommand(), "sbatch"), Program: a.GetProgram(),
		Runtime: runtime, Poll: seconds(s.GetPollIntervalSeconds(), clustercli.DefaultPoll),
	})
	if err != nil {
		return nil, nil, err
	}
	var scripts map[string]string
	if pl.GetResources().GetRocm() != nil {
		scripts = map[string]string{gpuScript: amd.Script}
	}
	sizing := apptainer.Sizing{
		Partition: s.GetPartition(), Gres: s.GetGres(), SharedGres: s.GetSharedGres(), Exclusive: exclusiveOf(s),
		CPUs: int(s.GetCpus()), Memory: s.GetMemory(), Arguments: a.GetSubmitArguments(),
	}
	eng, err := apptainer.New(apptainer.Config{
		Local: local, Export: local, Cluster: cluster, Root: pl.GetRemote().GetRemoteWorkspaceRoot(), Scope: scope, Runs: runs,
		Timers: clk, Clock: clk, Poll: seconds(s.GetPollIntervalSeconds(), apptainer.DefaultPoll),
		QueueTimeout: seconds(s.GetQueueTimeoutSeconds(), apptainer.DefaultQueueTimeout),
		Sizing:       sizing,
		NodeScripts:  scripts, Runtime: runtime, Converter: converter, LocalDir: localDir,
		Idle: seconds(a.GetIdleReleaseSeconds(), 0), AllocationLimit: seconds(a.GetAllocationLimitSeconds(), 0),
		MaxTime:        seconds(s.GetMaxTimeSeconds(), 0),
		ConvertTimeout: seconds(a.GetConvertTimeoutSeconds(), apptainer.DefaultConvertTimeout),
	})
	if err != nil {
		return nil, nil, err
	}
	cfg.Engine, cfg.Backend, cfg.Probe = eng, eng.Backend(), p.probe()
	// A job started in a standing allocation names it by this label, and its container runs inside it.
	cfg.AllocationLabel = apptainer.LabelAllocation
	unread := map[string]string{amd.FactModel: apptainer.Unread, amd.FactCount: apptainer.Unread, amd.FactDriver: apptainer.Unread}
	facts := apptainer.Facts{Engine: eng, Script: gpuScript, Parse: amd.Parse, Unread: unread}
	// The GPUs a job gets are its own allocation's: Slurm keeps two allocations off one GPU, and
	// gives a measure of [slurm] exclusive the whole node. So they are claimed per placement, named
	// by the placement and the cluster's login node: a measure holds its own placement's GPUs alone,
	// and the jobs of the cluster's other placements run beside it, in allocations of their own.
	if cfg.Resources, err = resources(pl.GetResources(), ssh, ssh.Host, cfg.Placement+"@"+ssh.Host, clk, facts); err != nil {
		return nil, nil, err
	}
	provider, err := docker.New(cfg)
	if err != nil {
		return nil, nil, err
	}
	return provider, &clusterSide{
		Cluster: cluster, Scope: scope, Sizing: sizing, MaxTime: seconds(s.GetMaxTimeSeconds(), 0), Partition: s.GetPartition(),
	}, nil
}

// resources are the resources of a placement, each made by its own package from its section. where
// names the machine the placement reaches, and gpus whose GPUs a job of it gets, which names their
// claim. The AMD GPUs' facts are read by amdFacts.
func resources(r *cliv1.Resources, shell remote.Shell, where, gpus string, clk clock.System, amdFacts rocm.Facts) ([]resource.Resource, error) {
	var out []resource.Resource
	if r.GetRocm() != nil {
		res, err := rocm.New(rocm.Config{Machine: gpus, Facts: amdFacts})
		if err != nil {
			return nil, err
		}
		out = append(out, res)
	}
	if g := r.GetGpu(); g != nil {
		res, err := gpu.New(gpu.Config{Devices: g.GetDevices(), Runtime: g.GetRuntime(), Machine: gpus, Facts: &nvidia.GPUs{Shell: shell, Timers: clk}})
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

// localScratch is where this machine converts an apptainer placement's images: the config's
// directory, or apptainer/ under the user's cache directory (XDG_CACHE_HOME, or ~/.cache).
func (p Process) localScratch(a *cliv1.ApptainerSettings) string {
	if dir := a.GetLocalScratchDir(); dir != "" {
		return dir
	}
	cache := p.Getenv("XDG_CACHE_HOME")
	if !path.IsAbs(cache) {
		cache = path.Join(p.Home, ".cache")
	}
	return path.Join(cache, "vibesys-cli", "apptainer")
}

// seconds is a wait the config gives in seconds, or the default when it gives none.
func seconds(s float64, otherwise time.Duration) time.Duration {
	if s > 0 {
		return time.Duration(s * float64(time.Second))
	}
	return otherwise
}
