package search

import (
	"cmp"
	"maps"
	"slices"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// The limits of what the view carries of ideas and workstreams, which are the limits the contract
// states for them.
const (
	maxPlans        = 200
	maxWorkstreams  = 100
	maxViews        = maxWorkstreams + 1 // one for each workstream, and one for the ideas of none
	maxCandidates   = 200
	maxGainsPerIdea = 8
)

// This file derives ideas from the plans of a task. A plan belongs to a repository and its task, and
// carries what an earlier run learned into the next, so the view holds the task's plans; what is the
// run's about them is derived here, once, by the rules below:
//
//   - Which run a plan belongs to is what was recorded of it, and only that: it is in the run when
//     the run made it (Plan.origin) or set one of its statuses (Plan.transitions). Never when a plan
//     was made or edited, and never because the run's jobs ran while it was open.
//   - Which candidates are an idea's: the run's measure jobs whose Job.plans name it, once each, as
//     the one projection of measure results (measurements) places them. A plan that no run's job
//     names has none.
//   - A gain is a candidate's value against the run's first measurement of the objective, oriented
//     so that more than 1 is better (gainRatio). The values are the projection's, never read again.
//   - A verdict is derived from the plan's status alone (verdictOf).

// verdictOf is where an idea stands, from the plan's status. It is total: a status the plan never
// set (UNSPECIFIED, a plan saved before plans had one) and one this build does not know are open,
// because a plan that nobody settled is still to be decided.
func verdictOf(status cli.PlanStatus) pb.IdeaVerdict {
	switch status {
	case cli.PlanStatus_PLAN_STATUS_WON:
		return pb.IdeaVerdict_IDEA_VERDICT_KEPT
	case cli.PlanStatus_PLAN_STATUS_REJECTED:
		return pb.IdeaVerdict_IDEA_VERDICT_REJECTED
	case cli.PlanStatus_PLAN_STATUS_PARKED:
		return pb.IdeaVerdict_IDEA_VERDICT_PARKED
	default:
		return pb.IdeaVerdict_IDEA_VERDICT_OPEN
	}
}

// inRun says the run made the plan or set one of its statuses, by what the plan recorded. A call
// from outside any run records no run id, so it is in no run; a plan saved before origins were
// recorded has neither and is in none.
func inRun(p *cli.Plan, run string) bool {
	if run == "" {
		return false
	}
	return p.GetOrigin().GetRunId() == run || slices.ContainsFunc(p.GetTransitions(), func(t *cli.PlanTransition) bool {
		return t.GetOrigin().GetRunId() == run
	})
}

// idea is one plan with what is derived of it, before any limit is applied.
type idea struct {
	plan *cli.Plan
	view *pb.IdeaView
	// relevant says the plan matters to the run: it is the run's by origin, or one of the run's
	// measure jobs names it. Relevant plans are chosen before the others when there are too many.
	relevant bool
	// cut says some candidate ids were left out of the view.
	cut bool
}

// ideas derives every plan of the facts for the run, ascending by number, from the same
// measurements the series are drawn from, and counts the run's measured candidates that name no plan
// (once for each job). A plan number appears once: the last fact of it holds.
func ideas(facts []*cli.TaskFact, run string) (out []*idea, unplanned uint32) {
	plans := latestPlans(facts)
	all := measurements(facts, run)
	linked := linkedPlans(facts, run)
	baselines := baselineValues(all)
	for _, number := range slices.Sorted(maps.Keys(plans)) {
		p := plans[number]
		var candidates []*measured
		for _, m := range all {
			if slices.Contains(linked[m.jobID], number) {
				candidates = append(candidates, m)
			}
		}
		view := &pb.IdeaView{PlanNumber: number, Verdict: verdictOf(p.GetStatus()), InRun: inRun(p, run), Best: gains(candidates, baselines)}
		cut := len(candidates) > maxCandidates
		for _, m := range candidates[max(0, len(candidates)-maxCandidates):] {
			view.CandidateJobIds = append(view.CandidateJobIds, m.jobID)
		}
		out = append(out, &idea{plan: p, view: view, relevant: view.InRun || len(candidates) > 0, cut: cut})
	}
	for _, m := range all {
		if len(linked[m.jobID]) == 0 {
			unplanned++
		}
	}
	return out, unplanned
}

// latestPlans are the plans of the facts by number: a plan number appears once, and the last fact of
// it holds.
func latestPlans(facts []*cli.TaskFact) map[uint32]*cli.Plan {
	plans := map[uint32]*cli.Plan{}
	for _, f := range facts {
		if p := f.GetPlan(); p != nil && p.GetNumber() != 0 {
			plans[p.GetNumber()] = p
		}
	}
	return plans
}

// linkedPlans are the plan numbers each job of the run names.
func linkedPlans(facts []*cli.TaskFact, run string) map[string][]uint32 {
	out := map[string][]uint32{}
	for _, f := range facts {
		if j := f.GetJob(); j != nil && submittedBy(j, run) {
			out[j.GetId()] = j.GetPlans()
		}
	}
	return out
}

// baselineValues are the run's first measurement of each objective, which the measurements, oldest
// first, give in order.
func baselineValues(all []*measured) map[string]objectiveValue {
	out := map[string]objectiveValue{}
	for _, m := range all {
		for name, v := range m.values {
			if _, seen := out[name]; !seen {
				out[name] = v
			}
		}
	}
	return out
}

// gains are the best value among the candidates on each objective, against the baseline, by metric
// name. An objective whose gain cannot be stated (see gainRatio) is left out.
func gains(candidates []*measured, baselines map[string]objectiveValue) []*pb.ObjectiveGain {
	best := map[string]objectiveValue{}
	for _, m := range candidates {
		for name, v := range m.values {
			if was, seen := best[name]; !seen || better(v.direction, v.value, was.value) {
				best[name] = v
			}
		}
	}
	var out []*pb.ObjectiveGain
	for _, name := range slices.Sorted(maps.Keys(best)) {
		base, ok := baselines[name]
		if !ok {
			continue
		}
		if ratio, stated := gainRatio(best[name].direction, best[name].value, base.value); stated {
			out = append(out, &pb.ObjectiveGain{Metric: name, Ratio: ratio, BestValue: best[name].value, BaselineValue: base.value})
		}
	}
	return out[:min(len(out), maxGainsPerIdea)]
}

// gainRatio is how much better the value is than the baseline, oriented so that more than 1 is
// better whichever way the objective improves: value over baseline when it is maximized, baseline
// over value when it is minimized. A ratio of numbers that are not above zero says nothing, so none
// is stated.
func gainRatio(direction cli.Direction, value, baseline float64) (float64, bool) {
	if !(value > 0 && baseline > 0) || !finite(value) || !finite(baseline) {
		return 0, false
	}
	ratio := value / baseline
	if direction == cli.Direction_DIRECTION_MINIMIZE {
		ratio = baseline / value
	}
	return ratio, finite(ratio) && ratio > 0
}

// chooseIdeas keeps at most maxPlans, newest first: the relevant ones are chosen before the others,
// the highest numbers of each first, so a run's own ideas are never cut for an older task's. It
// reports whether any were left out.
func chooseIdeas(all []*idea) (kept []*idea, truncated bool) {
	// all is ascending by number; keepFirst keeps the newest that matter first.
	kept = keepFirst(all, maxPlans, func(i *idea) bool { return i.relevant })
	slices.Reverse(kept)
	truncated = len(kept) < len(all)
	for _, i := range kept {
		truncated = truncated || i.cut
	}
	return kept, truncated
}

// workstreamViews count the ideas of the run, ascending by workstream number: a plan that is in the
// run (inRun) counts once, under its verdict, in its workstream. The best of a workstream is the
// best gain on each objective among its counted ideas. A run with no idea has no view.
func workstreamViews(all []*idea) (views []*pb.WorkstreamView, truncated bool) {
	byNumber := map[uint32]*pb.WorkstreamView{}
	best := map[uint32]map[string]*pb.ObjectiveGain{}
	for _, i := range all {
		if !i.view.InRun {
			continue
		}
		n := i.plan.GetWorkstream()
		v := byNumber[n]
		if v == nil {
			v, best[n] = &pb.WorkstreamView{Number: n}, map[string]*pb.ObjectiveGain{}
			byNumber[n] = v
		}
		switch i.view.Verdict {
		case pb.IdeaVerdict_IDEA_VERDICT_KEPT:
			v.Kept++
		case pb.IdeaVerdict_IDEA_VERDICT_REJECTED:
			v.Rejected++
		case pb.IdeaVerdict_IDEA_VERDICT_PARKED:
			v.Parked++
		default:
			v.Open++
		}
		for _, g := range i.view.Best {
			if was := best[n][g.Metric]; was == nil || g.Ratio > was.Ratio {
				best[n][g.Metric] = g
			}
		}
	}
	numbers := slices.Sorted(maps.Keys(byNumber))
	// Past the limit the newest workstreams stay, and the ideas of none (number 0) are the oldest.
	if len(numbers) > maxViews {
		numbers, truncated = numbers[len(numbers)-maxViews:], true
	}
	for _, n := range numbers {
		v := byNumber[n]
		for _, name := range slices.Sorted(maps.Keys(best[n])) {
			v.Best = append(v.Best, best[n][name])
		}
		// The field holds at most maxGainsPerIdea, as a single idea's best does; a workstream's ideas
		// can together measure more objectives than one, so the union is cut the same way its parts are.
		v.Best = v.Best[:min(len(v.Best), maxGainsPerIdea)]
		views = append(views, v)
	}
	return views, truncated
}

// chooseWorkstreams keeps at most maxWorkstreams of the task's workstreams, ascending by number: those
// the run's ideas belong to first, then the others, the newest of each first.
func chooseWorkstreams(facts []*cli.TaskFact, all []*idea) (kept []*cli.Workstream, truncated bool) {
	used := map[uint32]bool{}
	for _, i := range all {
		if i.view.InRun {
			used[i.plan.GetWorkstream()] = true
		}
	}
	byNumber := map[uint32]*cli.Workstream{}
	for _, f := range facts {
		if w := f.GetWorkstream(); w != nil && w.GetNumber() != 0 {
			byNumber[w.GetNumber()] = w
		}
	}
	var rows []*cli.Workstream
	for _, n := range slices.Sorted(maps.Keys(byNumber)) {
		rows = append(rows, byNumber[n])
	}
	kept = keepFirst(rows, maxWorkstreams, func(w *cli.Workstream) bool { return used[w.GetNumber()] })
	slices.SortFunc(kept, func(a, b *cli.Workstream) int { return cmp.Compare(a.GetNumber(), b.GetNumber()) })
	return kept, len(kept) < len(rows)
}
