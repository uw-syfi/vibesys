package alloc

import (
	"fmt"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Refusal is why Step refused a request, or dropped a job. Each is a type of its own, so the shell
// branches on the type and never on text; Message is display text that names what is at fault.
type Refusal interface {
	Message() string
	refusal()
}

// NotEnabled: the placement's operator config has no allocations section.
type NotEnabled struct {
	Placement string
	// Key is the section that would enable it, placements.<name>.allocations.
	Key string
}

// RunNotLive: the run has ended, and asks for nothing more.
type RunNotLive struct{ Run core.RunID }

// ForNotPositive: an allocation or a successor asked for no time.
type ForNotPositive struct{ For time.Duration }

// ForExceedsMaxFor: an allocation or a successor asked for longer than the placement allows.
type ForExceedsMaxFor struct {
	For, MaxFor time.Duration
	// Key is the setting that caps it, placements.<name>.allocations.max_for.
	Key string
}

// MaxHeldReached: the run holds as many allocations of the placement as it may.
type MaxHeldReached struct {
	Placement     string
	Held, MaxHeld int
}

// UnknownAllocation: no allocation has the ID.
type UnknownAllocation struct{ ID ID }

// NotOwnedByRun: the allocation is another run's.
type NotOwnedByRun struct {
	ID         ID
	Owner, Run core.RunID
}

// NotRunning: a job can go only into a Running allocation, and this one is in another stage. For
// an Ended one, EndReason says why it ended.
type NotRunning struct {
	ID        ID
	Stage     Stage
	EndReason EndReason
}

// NotEligible: the allocation's placement does not give the job what it needs.
type NotEligible struct {
	ID        ID
	Placement string
}

// DoesNotFit: the work ahead of the job and its own bound are longer than the time the allocation
// has left, less the margin.
type DoesNotFit struct {
	ID       ID
	TimeLeft time.Duration
	Ahead    time.Duration
	Bound    time.Duration
	Margin   time.Duration
}

// BoundNotPositive: a job said it takes no time, which no fit can judge.
type BoundNotPositive struct {
	Job   core.JobID
	Bound time.Duration
}

// DuplicateJob: the job is already held by an allocation.
type DuplicateJob struct {
	Job core.JobID
	ID  ID
}

// NotRenewable: only a Pending or Running allocation takes a renewal order.
type NotRenewable struct {
	ID    ID
	Stage Stage
}

// NoEnd: the allocation has no end, so there is nothing to renew.
type NoEnd struct{ ID ID }

// AlreadyRenewed: the allocation's order already made its one successor; renew that one instead.
type AlreadyRenewed struct{ ID, By ID }

// LeadNotPositive: a renewal order's lead is not positive.
type LeadNotPositive struct{ Lead time.Duration }

// LeadNotShorterThanFor: a renewal order's lead is at least its successor's time, so every
// successor would be due for renewal as it started.
type LeadNotShorterThanFor struct{ Lead, For time.Duration }

// UntilPassed: a renewal order's until is not after now.
type UntilPassed struct{ At, Now time.Time }

// NoUntil: a renewal order says nothing of when it stops.
type NoUntil struct{}

// Message implements Refusal.
func (r NotEnabled) Message() string {
	return fmt.Sprintf("placement %q offers no standing allocations: its operator config has no [%s] section", r.Placement, r.Key)
}

// Message implements Refusal.
func (r RunNotLive) Message() string { return fmt.Sprintf("run %q has ended", r.Run) }

// Message implements Refusal.
func (r ForNotPositive) Message() string {
	return fmt.Sprintf("an allocation must be asked for a positive time, not %s", r.For)
}

// Message implements Refusal.
func (r ForExceedsMaxFor) Message() string {
	return fmt.Sprintf("asked for %s, longer than %s = %s", r.For, r.Key, r.MaxFor)
}

// Message implements Refusal.
func (r MaxHeldReached) Message() string {
	return fmt.Sprintf("the run holds %d allocations of placement %q, and max_held is %d", r.Held, r.Placement, r.MaxHeld)
}

// Message implements Refusal.
func (r UnknownAllocation) Message() string { return fmt.Sprintf("no allocation %s", r.ID) }

// Message implements Refusal.
func (r NotOwnedByRun) Message() string {
	return fmt.Sprintf("allocation %s is held by run %q, not run %q", r.ID, r.Owner, r.Run)
}

// Message implements Refusal.
func (r NotRunning) Message() string {
	if r.Stage == StageEnded {
		return fmt.Sprintf("allocation %s has ended (%s) and takes no job", r.ID, r.EndReason)
	}
	return fmt.Sprintf("allocation %s is %s and takes no job; only a running one does", r.ID, r.Stage)
}

// Message implements Refusal.
func (r NotEligible) Message() string {
	return fmt.Sprintf("allocation %s is on placement %q, which does not give the job what it needs", r.ID, r.Placement)
}

// Message implements Refusal.
func (r DoesNotFit) Message() string {
	return fmt.Sprintf("the job does not fit allocation %s: it has %s left, less a margin of %s, and the job needs %s after %s of jobs ahead of it",
		r.ID, r.TimeLeft, r.Margin, r.Bound, r.Ahead)
}

// Message implements Refusal.
func (r BoundNotPositive) Message() string {
	return fmt.Sprintf("job %s has no positive bound (%s), so no fit can be judged", r.Job, r.Bound)
}

// Message implements Refusal.
func (r DuplicateJob) Message() string {
	return fmt.Sprintf("job %s is already held by allocation %s", r.Job, r.ID)
}

// Message implements Refusal.
func (r NotRenewable) Message() string {
	return fmt.Sprintf("allocation %s is %s; only a pending or running one takes a renewal order", r.ID, r.Stage)
}

// Message implements Refusal.
func (r NoEnd) Message() string {
	return fmt.Sprintf("allocation %s has no end, so there is nothing to renew", r.ID)
}

// Message implements Refusal.
func (r AlreadyRenewed) Message() string {
	return fmt.Sprintf("allocation %s was already renewed by %s; give %s the order instead", r.ID, r.By, r.By)
}

// Message implements Refusal.
func (r LeadNotPositive) Message() string {
	return fmt.Sprintf("a renewal's lead must be positive, not %s", r.Lead)
}

// Message implements Refusal.
func (r LeadNotShorterThanFor) Message() string {
	return fmt.Sprintf("a renewal's lead (%s) must be shorter than its --for (%s), or every successor would be due for renewal as it starts", r.Lead, r.For)
}

// Message implements Refusal.
func (r UntilPassed) Message() string {
	return fmt.Sprintf("a renewal's until (%s) is not after now (%s)", r.At.UTC().Format(time.RFC3339), r.Now.UTC().Format(time.RFC3339))
}

// Message implements Refusal.
func (NoUntil) Message() string {
	return "a renewal order needs an until: a time, or the end of the run"
}

func (NotEnabled) refusal()            {}
func (RunNotLive) refusal()            {}
func (ForNotPositive) refusal()        {}
func (ForExceedsMaxFor) refusal()      {}
func (MaxHeldReached) refusal()        {}
func (UnknownAllocation) refusal()     {}
func (NotOwnedByRun) refusal()         {}
func (NotRunning) refusal()            {}
func (NotEligible) refusal()           {}
func (DoesNotFit) refusal()            {}
func (BoundNotPositive) refusal()      {}
func (DuplicateJob) refusal()          {}
func (NotRenewable) refusal()          {}
func (NoEnd) refusal()                 {}
func (AlreadyRenewed) refusal()        {}
func (LeadNotPositive) refusal()       {}
func (LeadNotShorterThanFor) refusal() {}
func (UntilPassed) refusal()           {}
func (NoUntil) refusal()               {}
