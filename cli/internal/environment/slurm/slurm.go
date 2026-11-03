// Package slurm places the docker environment's containers in allocations of a Slurm cluster: a
// container's first process, and every image build, run under srun, so the node is held for them
// and for nothing else while they run.
//
// The Docker daemon runs outside Slurm's cgroups, so the end of an allocation does not end a
// container, and the end of a container does not end its allocation. Engine therefore removes
// both: its Remove removes the container and then cancels the allocation and confirms it ended,
// and a sweep of a scope finds allocations whose container is gone, by the job's name and
// comment, so the next run cancels what a dead one left.
//
// What it keeps of vs-slurm is the semantics, not the code: a job is never requeued (vs-slurm
// passes --no-requeue to sbatch; an allocation srun makes is interactive, and Slurm requeues batch
// jobs only, so srun has no such option), a job's state is read with squeue and then sacct, a
// cancel is confirmed, a run is found again by its job name, and an exit status nobody saw is
// unknown (the docker engine reports a transport's lost exit as lost).
package slurm

import (
	"context"
	"errors"
	"fmt"
	"io"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// JobPrefix starts the name of every Slurm job this package starts.
const JobPrefix = "vibesys-docker-"

// Margin is added to a command's limit to make its allocation's time limit: the allocation is a
// backstop, and the environment ends the command first.
const Margin = 10 * time.Minute

// BuildLimit is the time limit of an image build's allocation.
const BuildLimit = 4 * time.Hour

// JobName is the name of the Slurm job that holds the container or the build with the name.
func JobName(name string) string { return JobPrefix + name }

// Scheduler is the cluster's scheduler as Engine uses it.
type Scheduler interface {
	// Jobs returns the names of the user's jobs that this package started for the scope (their
	// comment), pending or running.
	Jobs(ctx context.Context, scope string) ([]string, error)
	// Cancel cancels the user's job of the name, when there is one, and returns once the scheduler
	// confirms it is no longer pending or running.
	Cancel(ctx context.Context, name string) error
}

// DefaultCPUs is how many CPUs an allocation that does not hold the whole node asks for.
const DefaultCPUs = 8

// Allocator is the dockercli.Allocator that runs a command under srun in a new allocation, sized
// by what the command is:
//
//   - An image build asks for CPUs and memory only, and builds in a BuildKit builder of its own
//     whose container is confined to the CPUs and memory Slurm gave it (Docker's own builder
//     runs in the daemon, outside Slurm's cgroups, and cannot be confined).
//   - A container whose commands are not measured, a check, asks for the GPUs (Gres) and its
//     share of CPUs and memory, so it can run beside other work on the node.
//   - A measured container, a benchmark sample, asks for the GPUs and, when Exclusive, the whole
//     node, so nothing else runs while it is timed.
//
// Every container is confined to the CPUs of its allocation with docker run --cpuset-cpus, read
// from the allocation itself.
type Allocator struct {
	Partition string
	Gres      string
	// Exclusive gives a measured container the whole node.
	Exclusive bool
	// CPUs is how many CPUs a build or an unmeasured container asks for; zero means DefaultCPUs.
	CPUs int
	// Memory, when set, is the memory (a Slurm size, such as "32G") a build or an unmeasured
	// container asks for, and the limit of a build's builder.
	Memory string
	// Arguments are passed to srun as they are, after the ones above.
	Arguments []string
	// Builder names the BuildKit builder of the daemon's builds: its state, the layer cache and
	// every RUN --mount=type=cache, is kept between builds under that name.
	Builder string
}

var _ dockercli.Allocator = Allocator{}

// Allocate implements dockercli.Allocator. The argv is the docker program, then its subcommand
// (build or run), then the subcommand's arguments.
func (a Allocator) Allocate(x dockercli.Allocation, argv []string) []string {
	limit := BuildLimit
	if !x.Build {
		limit = Margin
		if x.Limit > 0 {
			limit += x.Limit
		}
	}
	minutes := int((limit + time.Minute - 1) / time.Minute)
	out := []string{
		"srun", "--job-name=" + JobName(x.Name), "--nodes=1", "--ntasks=1",
		"--time=" + strconv.Itoa(minutes),
	}
	if x.Scope != "" {
		out = append(out, "--comment="+x.Scope)
	}
	if a.Partition != "" {
		out = append(out, "--partition="+a.Partition)
	}
	if !x.Build && a.Gres != "" {
		out = append(out, "--gres="+a.Gres)
	}
	if x.Measured && a.Exclusive {
		out = append(out, "--exclusive")
	} else {
		cpus := a.CPUs
		if cpus <= 0 {
			cpus = DefaultCPUs
		}
		out = append(out, "--cpus-per-task="+strconv.Itoa(cpus))
		if a.Memory != "" {
			out = append(out, "--mem="+a.Memory)
		}
	}
	out = append(out, a.Arguments...)
	out = append(out, "--")
	if len(argv) < 2 {
		return append(out, argv...)
	}
	if x.Build {
		return append(append(out, "sh", "-c", buildScript, "vs-build", a.builder(), a.Memory, argv[0]), argv[2:]...)
	}
	return append(append(out, "sh", "-c", confineScript, "vs-run", argv[0], argv[1]), argv[2:]...)
}

func (a Allocator) builder() string {
	if a.Builder != "" {
		return a.Builder
	}
	return dockercli.BuildPrefix + "builder"
}

// allowedCPUs prints the CPUs the allocation's step may run on, in cpuset syntax.
const allowedCPUs = `sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status`

// confineScript runs "$1 $2 --cpuset-cpus=<the step's CPUs> ${@:3}": the docker subcommand,
// confined to the allocation's CPUs. exec keeps the process the one srun started, so its stdin,
// signals and exit status are docker's.
const confineScript = `cpus=$(` + allowedCPUs + `)
program=$1; sub=$2; shift 2
exec "$program" "$sub" --cpuset-cpus="$cpus" "$@"`

// buildScript builds with "$3 buildx build --builder $1 --load ${@:4}" in a builder whose
// container is confined to the step's CPUs and to the memory $2 (none when empty). The builder is
// made for the build and removed after it with its state kept, so the next build under the same
// name starts from the same cache. A builder a killed build left is removed first.
const buildScript = `cpus=$(` + allowedCPUs + `)
builder=$1; memory=$2; program=$3; shift 3
opts="cpuset-cpus=$cpus"
[ -n "$memory" ] && opts="$opts,memory=$memory"
"$program" buildx rm --keep-state "$builder" >/dev/null 2>&1
trap '"$program" buildx rm --keep-state "$builder" >/dev/null 2>&1' EXIT
trap 'exit 143' TERM INT HUP
"$program" buildx create --name "$builder" --driver docker-container --driver-opt "$opts" >/dev/null || exit
"$program" buildx build --builder "$builder" --load "$@"`

// BuilderName is the name of the BuildKit builder of a daemon's builds, by its scope.
func BuilderName(scope string) string {
	return docker.Owner + "-builder-" + strings.ReplaceAll(scope, ".", "-")
}

// Builders removes the builder a build was confined in.
type Builders interface {
	// Remove removes the builder and keeps its state; a builder that is not there is no error.
	Remove(ctx context.Context) error
}

// Buildx is the Builders of a builder that the docker program reaches through a shell.
type Buildx struct {
	Shell   remote.Shell
	Timers  clock.Timers
	Program string
	Name    string
}

var _ Builders = Buildx{}

// Remove implements Builders.
func (b Buildx) Remove(ctx context.Context) error {
	bounded, done := remote.Bounded(ctx, b.Timers, CallTimeout)
	defer done()
	program := b.Program
	if program == "" {
		program = dockercli.DefaultProgram
	}
	script := `"$1" buildx inspect "$2" >/dev/null 2>&1 || exit 0; exec "$1" buildx rm --keep-state "$2"`
	if _, err := remote.Output(bounded, b.Shell, []string{"sh", "-c", script, "vs-builder", program, b.Name}, nil); err != nil {
		return fmt.Errorf("removing the builder %s: %w", b.Name, err)
	}
	return nil
}

// Engine is a docker.Engine whose containers and builds live in Slurm allocations: an inner engine
// that places them there (a dockercli.Engine with an Allocator), the scheduler that ends them,
// and the builder a build that did not finish leaves behind.
type Engine struct {
	Inner     docker.Engine
	Scheduler Scheduler
	// Builders, when set, removes the builder of a build that did not finish, after its
	// allocation ended.
	Builders Builders
}

var _ docker.Engine = Engine{}

// Image implements docker.Engine.
func (e Engine) Image(ctx context.Context, ref string) (docker.Image, bool, error) {
	return e.Inner.Image(ctx, ref)
}

// Build implements docker.Engine. A build that did not finish has its allocation cancelled, so a
// build cut short does not keep the node.
func (e Engine) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	err := e.Inner.Build(ctx, spec, log)
	if err == nil {
		return nil
	}
	cleanup := context.WithoutCancel(ctx)
	if cerr := e.Scheduler.Cancel(cleanup, JobName(dockercli.BuildName(spec.Tag))); cerr != nil {
		err = errors.Join(err, fmt.Errorf("and the build's allocation was not confirmed ended: %w", cerr))
	}
	if e.Builders != nil {
		if rerr := e.Builders.Remove(cleanup); rerr != nil {
			err = errors.Join(err, fmt.Errorf("and the build's builder was not removed: %w", rerr))
		}
	}
	return err
}

// Containers implements docker.Engine. With a scope among the labels, the containers of the
// scope's allocations are listed too, whether or not the container is there: an allocation whose
// container is gone is what a dead run left, and removing it cancels the allocation.
func (e Engine) Containers(ctx context.Context, match docker.Labels) ([]string, error) {
	names, err := e.Inner.Containers(ctx, match)
	if err != nil {
		return nil, err
	}
	scope, ok := match[docker.LabelScope]
	if !ok {
		return names, nil
	}
	jobs, err := e.Scheduler.Jobs(ctx, scope)
	if err != nil {
		return nil, err
	}
	for _, job := range jobs {
		name, ok := strings.CutPrefix(job, JobPrefix)
		if ok && !strings.HasPrefix(name, dockercli.BuildPrefix) && !slices.Contains(names, name) {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names, nil
}

// Remove implements docker.Engine: the container first, since the allocation's end would not end
// it, then the allocation, confirmed.
func (e Engine) Remove(ctx context.Context, name string) error {
	rmErr := e.Inner.Remove(ctx, name)
	if err := e.Scheduler.Cancel(ctx, JobName(name)); err != nil {
		return errors.Join(rmErr, fmt.Errorf("the allocation of %s was not confirmed ended: %w", name, err))
	}
	return rmErr
}

// Start implements docker.Engine. The inner engine places the container's first process in a new
// allocation and waits for it, so the wait includes the queue.
func (e Engine) Start(ctx context.Context, spec docker.ContainerSpec) (docker.Container, error) {
	return e.Inner.Start(ctx, spec)
}
