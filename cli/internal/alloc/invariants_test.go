package alloc_test

import (
	"errors"
	"fmt"
	"reflect"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

func coreRun(s string) core.RunID { return core.RunID(s) }
func coreJob(s string) core.JobID { return core.JobID(s) }

// next lists where each stage may go in one step: the arrows of the state diagram.
var next = map[alloc.Stage][]alloc.Stage{
	alloc.StagePending:  {alloc.StagePending, alloc.StageRunning, alloc.StageEnded},
	alloc.StageRunning:  {alloc.StageRunning, alloc.StageDraining, alloc.StageEnded},
	alloc.StageDraining: {alloc.StageDraining, alloc.StageEnded},
	alloc.StageEnded:    {alloc.StageEnded},
}

// checkStep checks every rule that holds across one step, from what went in and what came out. It
// is run after every step of every test in this package, so each test also checks all of them.
func checkStep(cfg alloc.Config, now time.Time, before alloc.State, e alloc.Event, after alloc.State, actions []alloc.Action) error {
	var errs []error
	fail := func(format string, args ...any) { errs = append(errs, fmt.Errorf(format, args...)) }

	// A refused request changes nothing, and says only that it was refused.
	if _, refused := refusal(actions); refused {
		if !reflect.DeepEqual(before, after) {
			fail("a refused request changed the state")
		}
		if len(actions) != 1 {
			fail("a refused request also asked for %v", kinds(actions))
		}
		return errors.Join(errs...)
	}

	prev := map[alloc.ID]alloc.Allocation{}
	for _, a := range before.Allocations() {
		prev[a.ID] = a
	}
	cur := map[alloc.ID]alloc.Allocation{}
	for _, a := range after.Allocations() {
		cur[a.ID] = a
	}
	submits := map[alloc.ID]bool{}
	for _, act := range actions {
		if s, ok := act.(alloc.Submit); ok {
			submits[s.Spec.ID] = true
		}
	}

	for id, b := range prev {
		a, ok := cur[id]
		if !ok {
			fail("%s disappeared", id)
			continue
		}
		// The stages only move forward, along the diagram; Ended is final.
		if !slices.Contains(next[b.Stage()], a.Stage()) {
			fail("%s went from %s to %s", id, b.Stage(), a.Stage())
		}
		if be, ok := b.Phase.(alloc.Ended); ok {
			ae, _ := a.Phase.(alloc.Ended)
			same := ae
			same.Released = be.Released
			if same != be || (be.Released && !ae.Released) {
				fail("%s changed after it ended: %+v to %+v", id, be, a.Phase)
			}
		}
		// An unreachable backend never ends anything.
		if _, ok := e.(alloc.Unreachable); ok && a.Stage() != b.Stage() {
			fail("%s went from %s to %s on an unreachable backend", id, b.Stage(), a.Stage())
		}
		// A successor is made at most once, and never replaced.
		if b.RenewedBy != "" && a.RenewedBy != b.RenewedBy {
			fail("%s was renewed by %s, and is now renewed by %q", id, b.RenewedBy, a.RenewedBy)
		}
		if b.Handle != (alloc.Handle{}) && a.Handle != b.Handle {
			fail("%s's handle changed from %+v to %+v", id, b.Handle, a.Handle)
		}
		if a.ID != b.ID || a.Run != b.Run || a.Placement != b.Placement || a.RequestedFor != b.RequestedFor || !a.RequestedAt.Equal(b.RequestedAt) {
			fail("%s's identity changed: %+v to %+v", id, b, a)
		}
	}

	all := after.Allocations()
	for i, a := range all {
		lim, enabled := cfg.Placements[a.Placement]
		b, old := prev[a.ID]
		if !a.ID.Valid() {
			fail("%q is not an ID the core makes", a.ID)
		}
		if i > 0 && !idBefore(all[i-1].ID, a.ID) {
			fail("%s is not numbered after %s", a.ID, all[i-1].ID)
		}
		if !old {
			// A new allocation is asked for now, Pending, and submitted.
			if a.Stage() != alloc.StagePending || !a.RequestedAt.Equal(now) || !submits[a.ID] {
				fail("new allocation %s is %s, asked at %s, submitted %v", a.ID, a.Stage(), a.RequestedAt, submits[a.ID])
			}
			if !enabled || a.RequestedFor <= 0 || a.RequestedFor > lim.MaxFor {
				fail("new allocation %s on %q for %s, which the placement does not allow", a.ID, a.Placement, a.RequestedFor)
			}
			if after.RunEnded(a.Run) {
				fail("new allocation %s for run %s, which has ended", a.ID, a.Run)
			}
		}
		// Every started allocation ends by its time limit, or has no end where the placement has none.
		if l, ok := liveOf(a); ok {
			switch {
			case lim.Endless && !l.EndsAt.IsZero():
				fail("%s on an endless placement ends at %s", a.ID, l.EndsAt)
			case !lim.Endless && l.EndsAt.IsZero():
				fail("%s is %s with no end", a.ID, a.Stage())
			case !lim.Endless && l.EndsAt.After(l.StartedAt.Add(a.RequestedFor)):
				fail("%s ends at %s, after its time limit", a.ID, l.EndsAt)
			}
			if l.StartedAt.Before(a.RequestedAt) || l.StartedAt.After(now) {
				fail("%s started at %s, outside its request (%s) and now", a.ID, l.StartedAt, a.RequestedAt)
			}
		}
		// A successor exists, is a different allocation of the same run and placement, and was made
		// within its order's until.
		if a.RenewedBy != "" {
			s, ok := cur[a.RenewedBy]
			switch {
			case !ok:
				fail("%s is renewed by %s, which does not exist", a.ID, a.RenewedBy)
			case s.ID == a.ID || s.Run != a.Run || s.Placement != a.Placement:
				fail("%s is renewed by %+v", a.ID, s)
			case !old || b.RenewedBy == "":
				// Made in this step: a new allocation, carrying the order, within its until.
				if _, existed := prev[s.ID]; existed {
					fail("%s is renewed by %s, which existed before", a.ID, s.ID)
				}
				if a.Renewal == nil || s.Renewal == nil || !reflect.DeepEqual(*a.Renewal, *s.Renewal) {
					fail("successor %s does not carry %s's order", s.ID, a.ID)
					break
				}
				if u, ok := a.Renewal.Until.(alloc.UntilTime); ok && !now.Before(u.At) {
					fail("%s made successor %s at %s, at or after its until %s", a.ID, s.ID, now, u.At)
				}
				if s.RequestedFor != a.Renewal.For {
					fail("successor %s asked for %s, its order says %s", s.ID, s.RequestedFor, a.Renewal.For)
				}
			}
		}
	}

	// A run holds no more than max_held allocations of a placement, renewals included.
	for _, a := range all {
		if lim, ok := cfg.Placements[a.Placement]; ok && after.Held(a.Run, a.Placement) > lim.MaxHeld {
			fail("run %s holds %d allocations of %s, more than %d", a.Run, after.Held(a.Run, a.Placement), a.Placement, lim.MaxHeld)
		}
	}

	// A job is held by at most one allocation, and each allocation runs at most one at a time.
	holders := map[core.JobID]alloc.ID{}
	for _, a := range all {
		l, ok := liveOf(a)
		if !ok {
			continue
		}
		jobs := []core.JobID{}
		if l.Current != nil {
			jobs = append(jobs, l.Current.Job)
		}
		for _, q := range l.Queue {
			jobs = append(jobs, q.Job)
		}
		for _, j := range jobs {
			if other, ok := holders[j]; ok {
				fail("job %s is held by %s and %s", j, other, a.ID)
			}
			holders[j] = a.ID
		}
	}

	for _, act := range actions {
		switch x := act.(type) {
		case alloc.StartJob:
			a := cur[x.ID]
			l, ok := liveOf(a)
			if !ok || l.Current == nil || l.Current.Job != x.Job {
				fail("StartJob %s in %s, which does not run it after the step", x.Job, x.ID)
				continue
			}
			// No job starts in an allocation it does not fit.
			if !l.EndsAt.IsZero() && x.Bound > l.EndsAt.Sub(now)-cfg.Placements[a.Placement].Margin {
				fail("%s started in %s with %s left, less the margin, and a bound of %s", x.Job, x.ID, l.EndsAt.Sub(now), x.Bound)
			}
			// First in, first out: every job queued ahead of it is gone from the queue.
			if bl, ok := liveOf(prev[x.ID]); ok {
				ahead := true
				for _, act := range actions {
					if n, ok := act.(alloc.NotPlaced); ok && n.Why == nil {
						fail("job %s was left without an allocation and nothing says why", n.Job)
					}
				}
				for _, q := range bl.Queue {
					if q.Job == x.Job {
						ahead = false
						continue
					}
					if ahead && slices.ContainsFunc(l.Queue, func(s alloc.Slot) bool { return s.Job == q.Job }) {
						fail("%s started in %s ahead of %s, which was queued before it", x.Job, x.ID, q.Job)
					}
				}
			}
		case alloc.LoseJob:
			b, a := prev[x.ID], cur[x.ID]
			bl, _ := liveOf(b)
			ae, ended := a.Phase.(alloc.Ended)
			if !ended || b.Stage() == alloc.StageEnded || bl.Current == nil || bl.Current.Job != x.Job || ae.Reason != x.Reason {
				fail("LoseJob %+v, and %s was %+v and is %+v", x, x.ID, b.Phase, a.Phase)
			}
		case alloc.Submit:
			if a, ok := cur[x.Spec.ID]; !ok || a.Stage() != alloc.StagePending || a.Handle != (alloc.Handle{}) ||
				x.Spec.For != a.RequestedFor || x.Spec.Run != a.Run || x.Spec.Placement != a.Placement {
				fail("Submit %+v of %+v", x.Spec, a)
			}
		case alloc.Release:
			a := cur[x.ID]
			if p, ok := a.Phase.(alloc.Ended); !ok || p.Released || a.Handle != x.Handle || x.Handle == (alloc.Handle{}) {
				fail("Release %+v of %+v", x, a)
			}
		case alloc.RecordWait:
		case alloc.DropJob, alloc.NotPlaced, alloc.Refuse:
		}
	}

	// A job running in an allocation that ends is lost with its end reason, and a job queued there
	// is dropped or placed elsewhere.
	finished := core.JobID("")
	if f, ok := e.(alloc.JobFinished); ok {
		finished = f.Job
	}
	for id, b := range prev {
		ae, ended := cur[id].Phase.(alloc.Ended)
		bl, live := liveOf(b)
		if !ended || !live {
			continue
		}
		if c := bl.Current; c != nil && c.Job != finished && !has(actions, alloc.LoseJob{Job: c.Job, ID: id, Reason: ae.Reason}) {
			fail("%s ended (%s) while %s ran in it, and the job was not lost", id, ae.Reason, c.Job)
		}
		for _, q := range bl.Queue {
			if q.Job != finished && !slices.ContainsFunc(actions, func(act alloc.Action) bool {
				switch x := act.(type) {
				case alloc.DropJob:
					return x.Job == q.Job
				case alloc.NotPlaced:
					return x.Job == q.Job
				}
				return false
			}) {
				fail("%s ended with %s queued, and the job was neither dropped nor placed elsewhere", id, q.Job)
			}
		}
	}

	// Every record is one the contract's rules accept.
	for _, a := range all {
		if err := wire.Validate(wire.FromAllocation(a)); err != nil {
			fail("%s breaks the contract: %v (%+v)", a.ID, err, a)
		}
	}
	return errors.Join(errs...)
}

func liveOf(a alloc.Allocation) (alloc.Live, bool) {
	switch p := a.Phase.(type) {
	case alloc.Running:
		return p.Live, true
	case alloc.Draining:
		return p.Live, true
	case alloc.Pending, alloc.Ended:
	}
	return alloc.Live{}, false
}

func idBefore(a, b alloc.ID) bool {
	return len(a) < len(b) || (len(a) == len(b) && a < b)
}
