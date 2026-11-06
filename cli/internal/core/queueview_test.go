package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var queueEpoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func randomTaskJobs(r *rand.Rand) ([]core.Job, []core.Plan) {
	var plans []core.Plan
	for n := range r.IntN(4) {
		plans = append(plans, core.Plan{Number: core.PlanNumber(n + 1), Priority: anyPriority[r.IntN(len(anyPriority))]})
	}
	kinds := []core.Settings{core.CheckSettings{}, core.MeasureSettings{Samples: 3}, core.CommandSettings{Argv: []string{"true"}}}
	var jobs []core.Job
	for i := range r.IntN(20) {
		j := core.Job{
			ID: core.JobID(fmt.Sprintf("j%d", i+1)), State: core.JobStates()[r.IntN(len(core.JobStates()))],
			Spec:        core.JobSpec{Settings: kinds[r.IntN(len(kinds))]},
			SubmittedAt: queueEpoch.Add(time.Duration(r.IntN(300)) * time.Minute), Priority: anyPriority[r.IntN(len(anyPriority))],
		}
		for range r.IntN(3) {
			j.Plans = append(j.Plans, core.PlanNumber(1+r.IntN(5)))
		}
		if j.State.Final() || j.State == core.JobRunning {
			started := j.SubmittedAt.Add(time.Duration(r.IntN(60)) * time.Minute)
			j.StartedAt = &started
		}
		if j.State.Final() {
			finished := j.StartedAt.Add(time.Duration(r.IntN(90)) * time.Minute)
			j.FinishedAt = &finished
		}
		jobs = append(jobs, j)
	}
	return jobs, plans
}

// Every queued and running job is in the queue once, in the group of the first of its plans that
// the task has, or of no plan, which comes after the plans' groups in number order. In a group the
// running jobs come first, then the queued ones by the priority they count as now, then by submit
// time; each has its age, and the estimate of its kind is the median run time of the task's
// succeeded jobs of that kind.
func TestTheQueueHasEveryWaitingJobOnceByPlanAndPriority(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 6))
	now := queueEpoch.Add(6 * time.Hour)
	for seed := range 2000 {
		jobs, plans := randomTaskJobs(r)
		view := core.QueueOf(jobs, plans, now)
		seen := map[core.JobID]bool{}
		last := core.PlanNumber(0)
		for gi, g := range view.Groups {
			if g.Plan == nil && gi != len(view.Groups)-1 {
				t.Fatalf("seed %d: the group of no plan is not last", seed)
			}
			if g.Plan != nil {
				if g.Plan.Number <= last {
					t.Fatalf("seed %d: plan %d after plan %d", seed, g.Plan.Number, last)
				}
				last = g.Plan.Number
			}
			for i, q := range g.Jobs {
				if seen[q.Job.ID] {
					t.Fatalf("seed %d: %s twice", seed, q.Job.ID)
				}
				seen[q.Job.ID] = true
				var want *core.PlanNumber
				for _, n := range q.Job.Plans {
					if slices.ContainsFunc(plans, func(p core.Plan) bool { return p.Number == n }) {
						want = &n
						break
					}
				}
				if (want == nil) != (g.Plan == nil) || (want != nil && *want != g.Plan.Number) {
					t.Fatalf("seed %d: %s with plans %v is in the group of %+v", seed, q.Job.ID, q.Job.Plans, g.Plan)
				}
				if q.Waited != now.Sub(q.Job.SubmittedAt) || q.Priority != core.AgedPriority(core.EffectivePriority(q.Job, plans), q.Waited) {
					t.Fatalf("seed %d: %s has age %v and priority %s", seed, q.Job.ID, q.Waited, q.Priority)
				}
				if est, n := core.DurationOf(jobs, q.Job.Spec.Settings.Kind()); est != q.Estimate || n != q.Finished {
					t.Fatalf("seed %d: %s estimate %v of %d, want %v of %d", seed, q.Job.ID, q.Estimate, q.Finished, est, n)
				}
				if i == 0 {
					continue
				}
				p := g.Jobs[i-1]
				switch {
				case p.Job.State == core.JobQueued && q.Job.State == core.JobRunning:
					t.Fatalf("seed %d: running %s after queued %s", seed, q.Job.ID, p.Job.ID)
				case p.Job.State == core.JobQueued && (p.Priority.Level() < q.Priority.Level() ||
					p.Priority == q.Priority && p.Job.SubmittedAt.After(q.Job.SubmittedAt)):
					t.Fatalf("seed %d: %s (%s) before %s (%s)", seed, p.Job.ID, p.Priority, q.Job.ID, q.Priority)
				}
			}
		}
		for _, j := range jobs {
			if (j.State == core.JobQueued || j.State == core.JobRunning) != seen[j.ID] {
				t.Fatalf("seed %d: %s (%s) in the queue: %v", seed, j.ID, j.State, seen[j.ID])
			}
		}
	}
}

// The estimate is the median run time of the succeeded jobs of a kind: the middle one, or between
// the two middle ones, and no estimate with none.
func TestTheEstimateIsTheMedianOfTheSucceededJobsOfAKind(t *testing.T) {
	ran := func(kind core.Settings, state core.JobState, minutes int) core.Job {
		start := queueEpoch
		end := start.Add(time.Duration(minutes) * time.Minute)
		return core.Job{State: state, Spec: core.JobSpec{Settings: kind}, StartedAt: &start, FinishedAt: &end}
	}
	check := core.CheckSettings{}
	history := []core.Job{
		ran(check, core.JobSucceeded, 10), ran(check, core.JobSucceeded, 30), ran(check, core.JobFailed, 1),
		ran(core.MeasureSettings{}, core.JobSucceeded, 99), ran(check, core.JobSucceeded, 20),
	}
	if d, n := core.DurationOf(history, core.JobCheck); d != 20*time.Minute || n != 3 {
		t.Fatalf("checks: %v of %d, want 20m of 3", d, n)
	}
	if d, n := core.DurationOf(append(history, ran(check, core.JobSucceeded, 40)), core.JobCheck); d != 25*time.Minute || n != 4 {
		t.Fatalf("four checks: %v of %d, want 25m of 4", d, n)
	}
	if d, n := core.DurationOf(history, core.JobCommand); d != 0 || n != 0 {
		t.Fatalf("commands: %v of %d, want no estimate", d, n)
	}
}
