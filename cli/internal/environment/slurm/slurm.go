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
// What it keeps of vs-slurm is the semantics, not the code: --no-requeue, a job's state read with
// squeue and then sacct, a cancel confirmed, a run found again by its job name, and an exit status
// nobody saw treated as unknown (the docker engine reports a transport's lost exit as lost).
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

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
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

// Allocator is the dockercli.Allocator that runs a command under srun in a new allocation.
type Allocator struct {
	Partition string
	Gres      string
	Exclusive bool
	// Arguments are passed to srun as they are, after the ones above.
	Arguments []string
}

var _ dockercli.Allocator = Allocator{}

// Allocate implements dockercli.Allocator.
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
		"srun", "--no-requeue", "--job-name=" + JobName(x.Name), "--nodes=1", "--ntasks=1",
		"--time=" + strconv.Itoa(minutes),
	}
	if x.Scope != "" {
		out = append(out, "--comment="+x.Scope)
	}
	if a.Partition != "" {
		out = append(out, "--partition="+a.Partition)
	}
	if a.Gres != "" {
		out = append(out, "--gres="+a.Gres)
	}
	if a.Exclusive {
		out = append(out, "--exclusive")
	}
	out = append(out, a.Arguments...)
	return append(append(out, "--"), argv...)
}

// Engine is a docker.Engine whose containers and builds live in Slurm allocations: an inner engine
// that places them there (a dockercli.Engine with an Allocator), and the scheduler that ends them.
type Engine struct {
	Inner     docker.Engine
	Scheduler Scheduler
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
	if cerr := e.Scheduler.Cancel(context.WithoutCancel(ctx), JobName(dockercli.BuildName(spec.Tag))); cerr != nil {
		return errors.Join(err, fmt.Errorf("and the build's allocation was not confirmed ended: %w", cerr))
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
