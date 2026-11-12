package app

import (
	"context"
	"fmt"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// AllocationView is an allocation with what the daemon knows of it besides its record.
type AllocationView struct {
	alloc.Allocation
	// Slurm says its placement's allocations are batch jobs of a Slurm cluster.
	Slurm bool
}

// AllocUntilKind is the state a wait on an allocation is for.
type AllocUntilKind string

// The states a wait is for.
const (
	// UntilRunning is running or draining.
	UntilRunning AllocUntilKind = "running"
	// UntilEndingIn is at most EndingIn of time left, or ended.
	UntilEndingIn AllocUntilKind = "ending_in"
	// UntilEnded is ended.
	UntilEnded AllocUntilKind = "ended"
)

// AllocUntil says what a wait on an allocation waits for.
type AllocUntil struct {
	Kind     AllocUntilKind
	EndingIn time.Duration
}

// AllocWaitOutcome is how a wait on an allocation ended.
type AllocWaitOutcome string

// The outcomes of a wait.
const (
	AllocWaitMet      AllocWaitOutcome = "met"
	AllocWaitTimedOut AllocWaitOutcome = "timed_out"
	// AllocWaitEnded is an allocation that ended before it was in the state asked for.
	AllocWaitEnded AllocWaitOutcome = "ended"
)

// errNoAllocations says the daemon keeps no standing allocations.
var errNoAllocations = &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon keeps no standing allocations: no placement of its operator config has an allocations section"}

func (a *App) allocations() (*allocShell, error) {
	if a.alloc == nil {
		return nil, errNoAllocations
	}
	return a.alloc, nil
}

// RefusalFailure is the failure a refusal of the allocation machine is. A request that cannot be
// right as it stands is invalid input, one the state of things refuses is a conflict, and an
// allocation nobody holds is not found.
func RefusalFailure(r alloc.Refusal) *core.Failure {
	kind := core.FailureConflict
	switch r.(type) {
	case alloc.NotEnabled, alloc.ForNotPositive, alloc.ForExceedsMaxFor, alloc.NotEligible, alloc.BoundNotPositive,
		alloc.LeadNotPositive, alloc.LeadNotShorterThanFor, alloc.UntilPassed, alloc.NoUntil:
		kind = core.FailureInvalidInput
	case alloc.UnknownAllocation, alloc.NotOwnedByRun:
		kind = core.FailureNotFound
	case alloc.RunNotLive, alloc.MaxHeldReached, alloc.NotRunning, alloc.DoesNotFit, alloc.DuplicateJob,
		alloc.NotRenewable, alloc.NoEnd, alloc.AlreadyRenewed, alloc.NoAllocation:
		kind = core.FailureConflict
	}
	return &core.Failure{Kind: kind, Message: r.Message()}
}

// RunSeen says the run made a call, so its allocations are not ended for its silence
// (alloc.Limits.RunIdleEnd). Every call that names a run says so; one that names none says
// nothing. It never blocks.
func (a *App) RunSeen(run core.RunID) {
	if a.alloc != nil && run != "" {
		a.alloc.post(allocMsg{event: alloc.RunSeen{Run: run, At: a.now()}})
	}
}

// ask gives the machine a request and returns the allocation it concerns.
func (s *allocShell) ask(ctx context.Context, e alloc.Event) (allocReply, error) {
	r, err := s.call(ctx, e)
	if err != nil {
		return allocReply{}, err
	}
	if r.refusal != nil {
		return allocReply{}, RefusalFailure(r.refusal)
	}
	return r, nil
}

func (s *allocShell) view(al alloc.Allocation) AllocationView {
	return AllocationView{Allocation: al, Slurm: s.slurm[al.Placement]}
}

// RequestAllocation asks for a standing allocation of a placement for a run.
func (a *App) RequestAllocation(ctx context.Context, run core.RunID, placement string, d time.Duration) (AllocationView, error) {
	a.RunSeen(run)
	s, err := a.allocations()
	if err != nil {
		return AllocationView{}, err
	}
	r, err := s.ask(ctx, alloc.Ask{Run: run, Placement: placement, For: d})
	if err != nil {
		return AllocationView{}, err
	}
	return s.view(r.allocation), nil
}

// Allocations lists the allocations of a run, or of every run when run is empty, oldest first. The
// ones that ended are listed only when ended is set.
func (a *App) Allocations(run core.RunID, ended bool) ([]AllocationView, error) {
	a.RunSeen(run)
	s, err := a.allocations()
	if err != nil {
		return nil, err
	}
	var out []AllocationView
	for _, al := range s.snapshot.Load().state.Allocations() {
		if (run == "" || al.Run == run) && (ended || al.Stage() != alloc.StageEnded) {
			out = append(out, s.view(al))
		}
	}
	return out, nil
}

// Allocation returns one allocation.
func (a *App) Allocation(id alloc.ID) (AllocationView, error) {
	s, err := a.allocations()
	if err != nil {
		return AllocationView{}, err
	}
	al, ok := s.snapshot.Load().state.Get(id)
	if !ok {
		return AllocationView{}, RefusalFailure(alloc.UnknownAllocation{ID: id})
	}
	return s.view(al), nil
}

// met says whether an allocation is in the state a wait is for, and the moment it will be if that
// is a moment, which is zero otherwise.
func (u AllocUntil) met(al alloc.Allocation, now time.Time) (bool, time.Time) {
	switch u.Kind {
	case UntilRunning:
		stage := al.Stage()
		return stage == alloc.StageRunning || stage == alloc.StageDraining, time.Time{}
	case UntilEnded:
		return al.Stage() == alloc.StageEnded, time.Time{}
	case UntilEndingIn:
		if al.Stage() == alloc.StageEnded {
			return true, time.Time{}
		}
		if l, ok := liveOf(al); ok && !l.EndsAt.IsZero() {
			at := l.EndsAt.Add(-u.EndingIn)
			return !now.Before(at), at
		}
	}
	return false, time.Time{}
}

// WaitAllocation returns once the allocation is in the state asked for, ended, or the timeout
// passed, whichever is first. It changes nothing.
func (a *App) WaitAllocation(ctx context.Context, id alloc.ID, until AllocUntil, timeout time.Duration) (AllocationView, AllocWaitOutcome, error) {
	s, err := a.allocations()
	if err != nil {
		return AllocationView{}, "", err
	}
	deadline, stopDeadline := a.cfg.Timers.Timeout(ctx, timeout)
	defer stopDeadline()
	for {
		wake := s.changes.Wait()
		al, ok := s.snapshot.Load().state.Get(id)
		if !ok {
			return AllocationView{}, "", RefusalFailure(alloc.UnknownAllocation{ID: id})
		}
		now := a.now()
		met, at := until.met(al, now)
		switch {
		case met:
			return s.view(al), AllocWaitMet, nil
		case al.Stage() == alloc.StageEnded:
			return s.view(al), AllocWaitEnded, nil
		}
		out, err := a.waitOnce(ctx, wake, deadline, at, now)
		if err != nil {
			return AllocationView{}, "", err
		}
		if out != "" {
			return s.view(al), out, nil
		}
	}
}

// waitOnce waits for the allocation to change, for a moment, or for the deadline, which is the only
// one that gives an outcome.
func (a *App) waitOnce(ctx context.Context, wake, deadline <-chan struct{}, at, now time.Time) (AllocWaitOutcome, error) {
	var moment <-chan struct{}
	if !at.IsZero() {
		var stop func()
		moment, stop = a.cfg.Timers.Timeout(ctx, at.Sub(now))
		defer stop()
	}
	select {
	case <-wake:
	case <-moment:
	case <-deadline:
		return AllocWaitTimedOut, nil
	case <-ctx.Done():
		return "", ctx.Err()
	}
	return "", nil
}

// EndAllocation ends an allocation of the run, at once or when its jobs are done.
func (a *App) EndAllocation(ctx context.Context, run core.RunID, id alloc.ID, afterJobs bool) (AllocationView, error) {
	a.RunSeen(run)
	s, err := a.allocations()
	if err != nil {
		return AllocationView{}, err
	}
	r, err := s.ask(ctx, alloc.EndAsk{Run: run, ID: id, AfterJobs: afterJobs})
	if err != nil {
		return AllocationView{}, err
	}
	return s.view(r.allocation), nil
}

// RenewAllocation gives an allocation of the run a renewal order, or takes its order away when
// order is nil.
func (a *App) RenewAllocation(ctx context.Context, run core.RunID, id alloc.ID, order *alloc.RenewalOrder) (AllocationView, error) {
	a.RunSeen(run)
	s, err := a.allocations()
	if err != nil {
		return AllocationView{}, err
	}
	var e alloc.Event = alloc.RenewOffAsk{Run: run, ID: id}
	if order != nil {
		e = alloc.RenewAsk{Run: run, ID: id, Order: *order}
	}
	r, err := s.ask(ctx, e)
	if err != nil {
		return AllocationView{}, err
	}
	return s.view(r.allocation), nil
}

// EndRunAllocations is the end of a run: each allocation of it that has not ended ends at once, and
// it asks for no more. It returns the run's allocations.
func (a *App) EndRunAllocations(ctx context.Context, run core.RunID) ([]AllocationView, error) {
	s, err := a.allocations()
	if err != nil {
		return nil, err
	}
	r, err := s.ask(ctx, alloc.RunEnded{Run: run})
	if err != nil {
		return nil, err
	}
	var out []AllocationView
	for _, al := range r.run {
		out = append(out, s.view(al))
	}
	return out, nil
}

// jobBound is the longest a job is taken to run, which is what it would ask a backend for on its
// own: a command's own timeout, and for a check or a measure what its task holds it to (flow.Bound),
// so a job of a task whose limits are minutes fits an allocation of minutes. A command that has not
// been through the daemon's flows yet is given the limit they will give it.
func (a *App) jobBound(ctx context.Context, scope core.Scope, spec core.JobSpec) (time.Duration, error) {
	read := func() (task.Task, error) {
		return a.taskAt(ctx, string(scope.Repository()), spec.Commit, string(scope.Task()))
	}
	switch s := spec.Settings.(type) {
	case core.CommandSettings:
		if s.Image == core.ImageHarness && spec.Environment != core.EnvironmentHost && s.Candidate == nil {
			// Perhaps not yet through the daemon's flows: beside a candidate's server, the server's
			// startup time is added to the command's limit.
			t, err := read()
			if err != nil {
				return 0, err
			}
			if s, err = resolveCommand(s, t, a.cfg.DefaultTimeout); err != nil {
				return 0, err
			}
		}
		if s.Timeout > 0 {
			return s.Timeout, nil
		}
		return a.cfg.DefaultTimeout, nil
	default:
		t, err := read()
		if err != nil {
			return 0, err
		}
		return flow.Bound(t, s), nil
	}
}

// precheckAllocation refuses a submission into an allocation that cannot work before anything is
// built or stored: the daemon keeps no allocations, the call has no run, or the allocation it names
// is not one that takes the job. What it cannot know yet, the job's eligible placements, is judged
// again when the job is stored.
func (a *App) precheckAllocation(ctx context.Context, req SubmitRequest) error {
	if req.Spec.Allocation == "" {
		return nil
	}
	s, err := a.allocations()
	if err != nil {
		return err
	}
	if req.Context.Run == "" {
		return invalid("allocation: a job submitted into an allocation belongs to a run; set --run or VIBESYS_RUN")
	}
	var eligible []string
	for _, p := range s.conf.Placements {
		eligible = append(eligible, p.Name)
	}
	into := alloc.ID(req.Spec.Allocation)
	if req.Spec.Allocation == core.AllocationAny {
		into = ""
	}
	bound, err := a.jobBound(ctx, req.Context.Scope, req.Spec)
	if err != nil {
		return err
	}
	_, actions := alloc.Step(s.snapshot.Load().state, s.cfg, a.now(), alloc.JobSubmitted{
		Run: req.Context.Run, Job: "precheck", Bound: bound, Into: into, Eligible: eligible,
	})
	for _, act := range actions {
		if r, ok := act.(alloc.Refuse); ok {
			return RefusalFailure(r.Reason)
		}
	}
	return nil
}

// submitIntoAllocation hands a stored job to the allocation machine. A job it refuses is cancelled
// and the refusal is returned, as if the submission had been refused at the door.
func (a *App) submitIntoAllocation(ctx context.Context, req SubmitRequest, ref lifecycle.Ref, job core.Job) error {
	s, err := a.allocations()
	if err != nil {
		return err
	}
	defer s.met(ref)
	var eligible []string
	for _, o := range a.cfg.Scheduler.Demand.Options(job) {
		if o.Placement != "" && !slices.Contains(eligible, o.Placement) {
			eligible = append(eligible, o.Placement)
		}
	}
	into := alloc.ID(job.Spec.Allocation)
	if job.Spec.Allocation == core.AllocationAny {
		into = ""
	}
	bound, err := a.jobBound(ctx, req.Context.Scope, job.Spec)
	if err == nil {
		_, err = s.ask(ctx, alloc.JobSubmitted{
			Run: req.Context.Run, Job: jobOfRef(ref), Bound: bound, Into: into, Eligible: eligible,
		})
	}
	if err == nil {
		return nil
	}
	if _, cancelErr := a.Cancel(ctx, req.Context, ref.Job, fmt.Sprintf("the allocation refused it: %v", err)); cancelErr != nil {
		a.cfg.Logf("cancelling job %s, which no allocation took: %v", ref, cancelErr)
	}
	return err
}

// PlacementSlots is the slots of a placement of the operator config, max_jobs of them, and how many
// the jobs and standing allocations that hold the machine leave free. ok is false for a placement
// the scheduler has no slots of.
func (a *App) PlacementSlots(placement string) (total, free uint32, ok bool) {
	id := schedule.PlacementOf(placement)
	for _, r := range a.cfg.Scheduler.Resources {
		if r.ID != id {
			continue
		}
		used := a.view.Load().used[id]
		return uint32(r.Capacity), uint32(max(r.Capacity-used, 0)), true
	}
	return 0, 0, false
}

// RecentWaits is how long recent allocations of the placement waited to start, newest first: the
// requests of this daemon, kept for as long as it runs and rebuilt from its log when it starts.
func (a *App) RecentWaits(placement string) []alloc.WaitSample {
	if a.alloc == nil {
		return nil
	}
	return slices.Clone(a.alloc.snapshot.Load().waits[placement])
}

// HasAllocations says whether the placement offers standing allocations.
func (a *App) HasAllocations(placement string) bool {
	if a.alloc == nil {
		return false
	}
	_, ok := a.alloc.cfg.Placements[placement]
	return ok
}
