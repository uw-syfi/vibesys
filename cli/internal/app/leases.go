package app

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// Leasing is how the daemon shares the resources its jobs hold alone with the daemons of other
// users and other machines: where each resource is leased, and who this daemon is to them. With no
// Book nothing is leased, and this daemon's scheduler is the only one to arbitrate its resources.
type Leasing struct {
	Book lease.Book
	// Holder names this daemon: its user, its machine and its process. Each lease adds the job and
	// the time.
	Holder core.LeaseHolder
}

// HeldLease is a lease this daemon holds for a job.
type HeldLease struct {
	Job      core.JobID
	Task     core.TaskName
	Resource schedule.ResourceID
	Key      lease.Key
	Where    string
	Since    time.Time
}

// LeaseConflict is a lease a job that already runs could not take, such as one a daemon took
// while this one was down: the job goes on, and the conflict is reported until the job ends.
type LeaseConflict struct {
	Job      core.JobID
	Resource schedule.ResourceID
	Holder   core.LeaseHolder
	Detail   string
}

// heldLease is a lease the loop keeps for a job.
type heldLease struct {
	resource schedule.ResourceID
	lease    lease.Lease
	where    string
	since    time.Time
}

// leaseBook keeps the leases the jobs hold, by job. Only the loop changes it; Status reads a copy.
type leaseBook struct {
	mu        sync.Mutex
	held      map[lifecycle.Ref][]heldLease
	conflicts map[lifecycle.Ref][]LeaseConflict
}

// taken is what an effect took for a job, for the loop to keep.
type taken struct {
	ref       lifecycle.Ref
	leases    []heldLease
	conflicts []LeaseConflict
}

// takeLeases takes the leases of the resources that other daemons share, in order, for a job that
// is about to start. On a refusal it gives back what it took and returns the refusal; nothing is
// held then.
func (a *App) takeLeases(ctx context.Context, ref lifecycle.Ref, resources []schedule.ResourceID) ([]heldLease, *lifecycle.LeaseRefused) {
	var got []heldLease
	for _, r := range resources {
		l, err := a.takeLease(ctx, ref, r)
		if err == nil {
			if l != nil {
				got = append(got, *l)
			}
			continue
		}
		a.giveBack(got)
		refused := &lifecycle.LeaseRefused{Ref: ref, Resource: r, At: a.now()}
		var held *lease.HeldError
		if errors.As(err, &held) {
			refused.Holder = held.Holder
		} else {
			refused.Detail = fmt.Sprintf("the lease of %s could not be taken: %v", r, err)
		}
		return nil, refused
	}
	return got, nil
}

// takeLease takes the lease of one resource, or returns nil for a resource no other daemon shares.
func (a *App) takeLease(ctx context.Context, ref lifecycle.Ref, r schedule.ResourceID) (*heldLease, error) {
	book := a.cfg.Leasing.Book
	if book == nil {
		return nil, nil
	}
	place, ok := book.Place(string(r))
	if !ok {
		return nil, nil
	}
	holder := a.cfg.Leasing.Holder
	holder.Job, holder.Since = ref.Job, a.now()
	bounded, done := clock.Deadline(ctx, a.cfg.Timers, a.cfg.CallTimeout)
	defer done()
	l, err := place.Leases.Acquire(bounded, place.Key, holder)
	if err != nil {
		return nil, err
	}
	return &heldLease{resource: r, lease: l, where: place.Leases.Where(), since: holder.Since}, nil
}

// adoptLeases takes, for a job that is adopted, the leases it does not hold yet, such as after a
// restart. A lease another holds does not stop a job that runs already: it is a conflict to report.
func (a *App) adoptLeases(ctx context.Context, ref lifecycle.Ref, missing []schedule.ResourceID) taken {
	out := taken{ref: ref}
	for _, r := range missing {
		l, err := a.takeLease(ctx, ref, r)
		switch {
		case err == nil && l != nil:
			out.leases = append(out.leases, *l)
		case err != nil:
			c := LeaseConflict{Job: ref.Job, Resource: r}
			var held *lease.HeldError
			if errors.As(err, &held) {
				c.Holder = held.Holder
			} else {
				c.Detail = err.Error()
			}
			a.cfg.Logf("job %s runs without the lease of %s: %v", ref, r, err)
			out.conflicts = append(out.conflicts, c)
		}
	}
	return out
}

// giveBack releases leases, each bounded by the call timeout. It runs on the caller's goroutine.
func (a *App) giveBack(ls []heldLease) {
	for _, l := range ls {
		ctx, done := clock.Deadline(context.Background(), a.cfg.Timers, a.cfg.CallTimeout)
		if err := l.lease.Release(ctx); err != nil {
			a.cfg.Logf("releasing the lease of %s on %s: %v", l.resource, l.where, err)
		}
		done()
	}
}

// keep records what an effect took for a job.
func (b *leaseBook) keep(ref lifecycle.Ref, t taken) {
	if len(t.leases) == 0 && len(t.conflicts) == 0 {
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.held == nil {
		b.held, b.conflicts = map[lifecycle.Ref][]heldLease{}, map[lifecycle.Ref][]LeaseConflict{}
	}
	b.held[ref] = append(b.held[ref], t.leases...)
	b.conflicts[ref] = append(b.conflicts[ref], t.conflicts...)
}

// missing are the resources of rs whose lease the job does not hold.
func (b *leaseBook) missing(ref lifecycle.Ref, rs []schedule.ResourceID) []schedule.ResourceID {
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []schedule.ResourceID
	for _, r := range rs {
		if !slices.ContainsFunc(b.held[ref], func(l heldLease) bool { return l.resource == r }) {
			out = append(out, r)
		}
	}
	return out
}

// letGo removes the leases of the jobs the machine does not hold any more, and returns them for the
// caller to release: a job's leases are held exactly while the machine holds its claims, so they are
// given back on every way a job ends, is refused, or leaves the machine.
func (b *leaseBook) letGo(holds func(lifecycle.Ref) bool) []heldLease {
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []heldLease
	for ref, ls := range b.held {
		if !holds(ref) {
			out = append(out, ls...)
			delete(b.held, ref)
			delete(b.conflicts, ref)
		}
	}
	for ref := range b.conflicts {
		if !holds(ref) {
			delete(b.conflicts, ref)
		}
	}
	return out
}

// all removes every lease, for the caller to release.
func (b *leaseBook) all() []heldLease {
	return b.letGo(func(lifecycle.Ref) bool { return false })
}

// snapshot lists what is held and what conflicts, by job.
func (b *leaseBook) snapshot(tasks map[lifecycle.StoreID]core.TaskName) ([]HeldLease, []LeaseConflict) {
	b.mu.Lock()
	defer b.mu.Unlock()
	var held []HeldLease
	var conflicts []LeaseConflict
	for ref, ls := range b.held {
		for _, l := range ls {
			held = append(held, HeldLease{Job: ref.Job, Task: tasks[ref.Store], Resource: l.resource, Key: l.lease.Key(), Where: l.where, Since: l.since})
		}
	}
	for _, cs := range b.conflicts {
		conflicts = append(conflicts, cs...)
	}
	slices.SortFunc(held, func(x, y HeldLease) int {
		if c := x.Since.Compare(y.Since); c != 0 {
			return c
		}
		return compareStrings(string(x.Job)+string(x.Resource), string(y.Job)+string(y.Resource))
	})
	slices.SortFunc(conflicts, func(x, y LeaseConflict) int {
		return compareStrings(string(x.Job)+string(x.Resource), string(y.Job)+string(y.Resource))
	})
	return held, conflicts
}

func compareStrings(x, y string) int {
	switch {
	case x < y:
		return -1
	case x > y:
		return 1
	}
	return 0
}
