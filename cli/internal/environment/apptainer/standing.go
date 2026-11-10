package apptainer

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// A standing allocation is a batch allocation a run holds over time and runs its jobs' containers in
// one job at a time (internal/alloc). It differs from the allocation of a container in who ends it:
// a container's allocation is the container's, held by the process that started it and cancelled
// when that process closes the container, dies or sweeps a dead run's leftovers; a standing one is
// the run's, and ends only by its own request (Allocator.Release) or by Slurm's time limit.
//
// What marks one, in squeue:
//
//   - its name is StandingName(id): StandingPrefix and the allocation's ID, which starts with
//     JobPrefix like every allocation this package asks for, so that Jobs lists it, and which
//     Engine.Containers passes over by StandingPrefix, as it does a reusable allocation, so that a
//     run's sweep never cancels it;
//   - its comment is RunComment(scope, run): the placement's scope and the run that owns it;
//   - it runs no holder, so no caller's exit cancels it, and its time limit is the requested one
//     (rounded down to whole minutes), which is the only backstop of a daemon that died.

// StandingPrefix starts the name of every standing allocation.
const StandingPrefix = JobPrefix + "standing-"

// StandingName is the name of the standing allocation of the ID.
func StandingName(id alloc.ID) string { return StandingPrefix + string(id) }

// LabelAllocation is the label of a container's spec that names the standing allocation the
// container's job runs in: the batch job's id (alloc.Handle.Ref). The job's first container joins
// that allocation (srun --jobid --overlap) instead of asking for one of its own, and its removal
// kills its processes and leaves the allocation. A container that joins another's allocation
// (docker network "container:...") has no need of it: it joins that one's, the standing one included.
const LabelAllocation = "io.vibesys.cli.allocation"

// runPattern is what a run's ID is made of, since it is part of a Slurm comment.
var runPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

// AllocatorConfig configures NewAllocator.
type AllocatorConfig struct {
	// Cluster is the cluster the allocations are asked of.
	Cluster Cluster
	// Scope is the placement's scope, the one Config.Scope of the placement's Engine names: it
	// starts every allocation's comment, which is how Adopt finds this daemon's own.
	Scope string
	// Sizing is what each allocation asks for. An allocation is sized for a measured container, so
	// any job may run in it.
	Sizing Sizing
	// MaxTime is the longest time limit the cluster grants, as Config.MaxTime; zero is no cap.
	MaxTime time.Duration
}

// Allocator is the environment.Allocator of a Slurm cluster whose nodes run Apptainer. Create it
// with NewAllocator. It is safe for concurrent use.
type Allocator struct{ cfg AllocatorConfig }

var _ environment.Allocator = (*Allocator)(nil)

// NewAllocator returns the allocator, or a core.FailureInvalidInput naming what is missing.
func NewAllocator(cfg AllocatorConfig) (*Allocator, error) {
	switch {
	case cfg.Cluster == nil:
		return nil, invalid("the allocator's config has no Cluster")
	case cfg.Scope == "" || strings.ContainsAny(cfg.Scope, "\n\x00"):
		return nil, invalid("the allocator's scope %q is not a scope", cfg.Scope)
	case cfg.MaxTime < 0 || cfg.MaxTime%time.Minute != 0:
		return nil, invalid("the longest allocation, %v, is not a whole number of minutes, as Slurm counts a time limit", cfg.MaxTime)
	}
	return &Allocator{cfg: cfg}, nil
}

// Request implements environment.Allocator: it submits the allocation unless the cluster has a live
// one of the spec's ID for its run, which it returns instead. The time limit is spec.For rounded
// down to whole minutes, so the allocation never outlives what was asked for; one shorter than a
// minute is refused.
func (a *Allocator) Request(ctx context.Context, s alloc.Spec) (alloc.Handle, error) {
	limit := s.For - s.For%time.Minute
	switch {
	case !s.ID.Valid():
		return alloc.Handle{}, invalid("%q is not an allocation ID", s.ID)
	case !runPattern.MatchString(string(s.Run)):
		return alloc.Handle{}, invalid("the run %q cannot be named in a Slurm comment", s.Run)
	case limit <= 0:
		return alloc.Handle{}, invalid("the allocation %s asks for %v, and Slurm counts whole minutes", s.ID, s.For)
	case a.cfg.MaxTime > 0 && limit > a.cfg.MaxTime:
		return alloc.Handle{}, invalid("the allocation %s asks for %v, longer than the placement's longest allocation, %v", s.ID, s.For, a.cfg.MaxTime)
	}
	comment := RunComment(a.cfg.Scope, string(s.Run))
	jobs, err := a.cfg.Cluster.Jobs(ctx, a.cfg.Scope)
	if err != nil {
		return alloc.Handle{}, err
	}
	for _, j := range jobs {
		if j.Name == StandingName(s.ID) && j.Comment == comment && j.Phase != Ended {
			return alloc.Handle{Alloc: s.ID, Ref: j.ID}, nil
		}
	}
	job, err := a.cfg.Cluster.Submit(ctx, Allocation{
		Name: StandingName(s.ID), Comment: comment, Limit: limit, Options: a.cfg.Sizing.Options(true),
	})
	if err != nil {
		return alloc.Handle{}, err
	}
	return alloc.Handle{Alloc: s.ID, Ref: job}, nil
}

// Observe implements environment.Allocator. A cluster that cannot be reached is an error of kind
// unavailable, and never an end; a job Slurm knows nothing of is ended. An end is by the time limit
// when Slurm says TIMEOUT, and otherwise "otherwise": a cancel by anyone, a node's failure or a
// job Slurm no longer keeps a record of.
func (a *Allocator) Observe(ctx context.Context, h alloc.Handle) (alloc.Observation, error) {
	if err := checkHandle(h); err != nil {
		return nil, err
	}
	d, err := a.cfg.Cluster.Detail(ctx, h.Ref)
	if err != nil {
		return nil, err
	}
	switch d.Phase {
	case Queued:
		return alloc.SeenPending{Reason: d.Reason, EstimatedStart: d.EstimatedStart}, nil
	case Running:
		return alloc.SeenRunning{Node: d.Node, StartedAt: d.StartedAt, EndsAt: d.EndsAt}, nil
	}
	cause := alloc.EndedOtherwise
	if d.TimeLimit {
		cause = alloc.EndedByTimeLimit
	}
	return alloc.SeenEnded{Cause: cause, At: d.EndedAt}, nil
}

// Release implements environment.Allocator: Slurm's cancel of the job, confirmed by squeue and then
// sacct. A job that has ended is released.
func (a *Allocator) Release(ctx context.Context, h alloc.Handle) error {
	if err := checkHandle(h); err != nil {
		return err
	}
	if err := a.cfg.Cluster.CancelJob(ctx, h.Ref); err != nil {
		return fmt.Errorf("releasing the allocation %s (job %s): %w", h.Alloc, h.Ref, err)
	}
	return nil
}

// Adopt implements environment.Allocator: the pending and running standing allocations of this
// scope, found by their name. A per-container allocation, a reusable one and any other job of the
// user's are not standing allocations.
func (a *Allocator) Adopt(ctx context.Context) ([]alloc.Handle, error) {
	jobs, err := a.cfg.Cluster.Jobs(ctx, a.cfg.Scope)
	if err != nil {
		return nil, err
	}
	var out []alloc.Handle
	for _, j := range jobs {
		id, ok := strings.CutPrefix(j.Name, StandingPrefix)
		if !ok || j.Phase == Ended || !alloc.ID(id).Valid() {
			continue
		}
		out = append(out, alloc.Handle{Alloc: alloc.ID(id), Ref: j.ID})
	}
	return out, nil
}

func checkHandle(h alloc.Handle) error {
	if h.Ref == "" || strings.Trim(h.Ref, "0123456789") != "" {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("apptainer: %q is not a batch job's id", h.Ref)}
	}
	return nil
}

// JobDetail is what Slurm says of one job, for a standing allocation: its state, and the times
// that go with it, as instants (the cluster's local times converted with the login node's clock).
type JobDetail struct {
	Phase Phase
	// Name is Slurm's name of the state, when it gave one.
	Name string
	// Reason is why a queued job waits.
	Reason string
	// EstimatedStart is when Slurm expects a queued job to start; zero when it does not say.
	EstimatedStart time.Time
	// Node is the node of a running job.
	Node string
	// StartedAt is when a running job started, and EndsAt when its time limit ends it; EndsAt is
	// zero for a job with no limit.
	StartedAt, EndsAt time.Time
	// EndedAt is when an ended job ended, when Slurm says.
	EndedAt time.Time
	// TimeLimit says an ended job ended by its time limit (Slurm's state TIMEOUT).
	TimeLimit bool
}
