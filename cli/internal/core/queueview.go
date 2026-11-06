package core

import (
	"slices"
	"time"
)

// QueueView is a task's queue as a lead reads it to decide what goes next: its queued and running
// jobs grouped by plan, each with what it is, how long it has waited and how long a job of its kind
// took in this task. It is derived from the store and the daemon's view; nothing keeps it.
type QueueView struct {
	// Groups are the plans that have a queued or running job, by plan number, then the jobs linked
	// to no plan.
	Groups []QueueGroup
}

// QueueGroup is the queued and running jobs of one plan, or with Plan nil of no plan, running jobs
// first, then the queued ones, each in the order the scheduler considers them: by priority, then by
// submit time.
type QueueGroup struct {
	Plan *Plan
	Jobs []QueuedJob
}

// QueuedJob is one queued or running job in a QueueView.
type QueuedJob struct {
	Job Job
	// Priority is the priority the job counts as now: its own or its plans', aged by its wait
	// (AgedPriority). It means something only while the job is queued.
	Priority Priority
	// Waited is how long since it was submitted.
	Waited time.Duration
	// Estimate is the median time a job of its kind took to run in this task, of Finished of them,
	// and Finished zero when none has: there is no estimate.
	Estimate time.Duration
	Finished int
}

// DurationOf is the median of how long the succeeded jobs of a kind ran, from start to end, and how
// many there were. With none, it is zero of zero: no estimate.
func DurationOf(history []Job, kind JobKind) (time.Duration, int) {
	var ds []time.Duration
	for _, j := range history {
		if j.State != JobSucceeded || j.Spec.Settings == nil || j.Spec.Settings.Kind() != kind || j.StartedAt == nil || j.FinishedAt == nil {
			continue
		}
		if d := j.FinishedAt.Sub(*j.StartedAt); d >= 0 {
			ds = append(ds, d)
		}
	}
	if len(ds) == 0 {
		return 0, 0
	}
	slices.Sort(ds)
	mid := len(ds) / 2
	if len(ds)%2 == 1 {
		return ds[mid], len(ds)
	}
	return ds[mid-1] + (ds[mid]-ds[mid-1])/2, len(ds)
}

// QueueOf builds the queue view of a task from its jobs, all of them (the finished ones are the
// history the estimates come from), and its plans, at a moment. A job is in the group of the first
// plan it is linked to that the plans name, and with none in the group of no plan.
func QueueOf(jobs []Job, plans []Plan, now time.Time) QueueView {
	byNumber := map[PlanNumber]Plan{}
	for _, p := range plans {
		byNumber[p.Number] = p
	}
	groups := map[PlanNumber][]QueuedJob{}
	for _, j := range jobs {
		if j.State != JobQueued && j.State != JobRunning {
			continue
		}
		var group PlanNumber
		for _, n := range j.Plans {
			if _, ok := byNumber[n]; ok {
				group = n
				break
			}
		}
		q := QueuedJob{Job: j, Waited: max(now.Sub(j.SubmittedAt), 0)}
		q.Priority = AgedPriority(EffectivePriority(j, plans), q.Waited)
		if j.Spec.Settings != nil {
			q.Estimate, q.Finished = DurationOf(jobs, j.Spec.Settings.Kind())
		}
		groups[group] = append(groups[group], q)
	}
	numbers := make([]PlanNumber, 0, len(groups))
	for n := range groups {
		numbers = append(numbers, n)
	}
	slices.Sort(numbers)
	// The group of no plan, number zero, goes last.
	if len(numbers) > 0 && numbers[0] == 0 {
		numbers = append(numbers[1:], 0)
	}
	var out QueueView
	for _, n := range numbers {
		js := groups[n]
		slices.SortStableFunc(js, func(a, b QueuedJob) int {
			if ra, rb := a.Job.State == JobRunning, b.Job.State == JobRunning; ra != rb {
				if ra {
					return -1
				}
				return 1
			}
			if c := b.Priority.Level() - a.Priority.Level(); c != 0 && a.Job.State == JobQueued {
				return c
			}
			return a.Job.SubmittedAt.Compare(b.Job.SubmittedAt)
		})
		g := QueueGroup{Jobs: js}
		if n != 0 {
			p := byNumber[n]
			g.Plan = &p
		}
		out.Groups = append(out.Groups, g)
	}
	return out
}
