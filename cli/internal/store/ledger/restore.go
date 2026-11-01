package ledger

import (
	"cmp"
	"fmt"
	"maps"
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// Restore rebuilds a Ledger from durable state, and fails with FailureInternal when the state
// breaks an invariant a store keeps: job ids and plan numbers are consecutive from 1, a result
// belongs to a job that succeeded and names it and a job that names a result has it, a plan link
// names a plan that exists, and only a job has a log. A value that no store would hold is refused
// too, so a record edited by hand cannot get past what Submit would have refused.
func Restore(rules store.Rules, snap Snapshot) (*Ledger, error) {
	l := New(rules)
	if err := l.restoreJobs(snap.Jobs); err != nil {
		return nil, err
	}
	if err := l.restorePlans(snap.Plans); err != nil {
		return nil, err
	}
	if err := l.restoreResults(snap.Results); err != nil {
		return nil, err
	}
	for _, job := range l.jobs {
		if _, ok := l.resultOf[job.ID]; !ok && job.ResultJob != "" {
			return nil, internal("job %s names result %s, which is not saved", job.ID, job.ResultJob)
		}
		for _, plan := range job.Plans {
			if !l.hasPlan(plan) {
				return nil, internal("job %s links plan %d, which does not exist", job.ID, plan)
			}
		}
	}
	for i, p := range snap.Promotions {
		held, err := Hold(fmt.Sprintf("promotion %d", i+1), p)
		if err != nil {
			return nil, err
		}
		l.promotions = append(l.promotions, held)
	}
	maps.Copy(l.events, snap.Events)
	for id, length := range snap.Logs {
		if _, ok := l.jobIndex[id]; !ok {
			return nil, internal("a log of %d bytes belongs to job %s, which does not exist", length, id)
		}
		l.logs[id] = length
	}
	return l, nil
}

func (l *Ledger) restoreJobs(jobs []core.Job) error {
	numbered := make([]uint64, len(jobs))
	order := make([]int, len(jobs))
	for i, job := range jobs {
		n, ok := JobNumber(job.ID)
		if !ok {
			return internal("job id %q is not one the store assigns", job.ID)
		}
		numbered[i], order[i] = n, i
	}
	slices.SortFunc(order, func(a, b int) int { return cmp.Compare(numbered[a], numbered[b]) })
	for want, i := range order {
		job, err := Hold("job "+string(jobs[i].ID), jobs[i])
		if err != nil {
			return err
		}
		if numbered[i] != uint64(want)+1 {
			return internal("job ids are not consecutive: %s is missing", JobIDFor(uint64(want)+1))
		}
		if err := checkStoredJob(job); err != nil {
			return err
		}
		l.putJob(job)
	}
	return nil
}

func checkStoredJob(job core.Job) error {
	if job.Spec.Settings == nil {
		return internal("job %s has no settings", job.ID)
	}
	if !slices.Contains(core.JobStates(), job.State) {
		return internal("job %s is in state %q, which is not a job state", job.ID, job.State)
	}
	if (job.Spec.Settings.Kind() == core.JobCommand) != (job.Key == "") {
		return internal("job %s is a %s job with key %q: only a command job has no key", job.ID, job.Spec.Settings.Kind(), job.Key)
	}
	if job.ResultJob != "" && job.ResultJob != job.ID {
		return internal("job %s names the result of job %s, and a job names only its own", job.ID, job.ResultJob)
	}
	return nil
}

func (l *Ledger) restorePlans(plans []core.Plan) error {
	ordered := slices.SortedFunc(slices.Values(plans), func(a, b core.Plan) int { return cmp.Compare(a.Number, b.Number) })
	for i, plan := range ordered {
		if plan.Number != core.PlanNumber(i+1) {
			return internal("plan numbers are not consecutive: plan %d is missing", i+1)
		}
		held, err := Hold(fmt.Sprintf("plan %d", plan.Number), plan)
		if err != nil {
			return err
		}
		l.plans = append(l.plans, held)
	}
	return nil
}

func (l *Ledger) restoreResults(results []SavedResult) error {
	ordered := slices.SortedFunc(slices.Values(results), func(a, b SavedResult) int { return cmp.Compare(a.Seq, b.Seq) })
	for i, saved := range ordered {
		held, err := Hold("result of job "+string(saved.Result.Job), saved)
		if err != nil {
			return err
		}
		if i > 0 && ordered[i-1].Seq == held.Seq {
			return internal("results of jobs %s and %s are both number %d", ordered[i-1].Result.Job, held.Result.Job, held.Seq)
		}
		j, ok := l.jobIndex[held.Result.Job]
		switch {
		case !ok:
			return internal("a result belongs to job %s, which does not exist", held.Result.Job)
		case l.jobs[j].ResultJob != l.jobs[j].ID:
			return internal("job %s has a saved result and names %q as its result", l.jobs[j].ID, l.jobs[j].ResultJob)
		case l.jobs[j].State != core.JobSucceeded:
			return internal("job %s is %s and has a saved result: only a job that succeeded has one", l.jobs[j].ID, l.jobs[j].State)
		case held.Result.Key != l.jobs[j].Key || held.Result.Outcome == nil:
			return internal("the result of job %s does not match the job", l.jobs[j].ID)
		}
		l.results = append(l.results, held)
		l.resultOf[held.Result.Job] = len(l.results) - 1
	}
	return nil
}
