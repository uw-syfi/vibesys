package core

import (
	"slices"
	"strings"
)

// CacheAction is what a submission does about the results already saved.
type CacheAction string

// The cache actions.
const (
	// CacheReuse answers the submission from saved results and runs nothing.
	CacheReuse CacheAction = "reuse"
	// CacheTopUp runs only the samples the saved results lack.
	CacheTopUp CacheAction = "top_up"
	// CacheRun runs the whole job.
	CacheRun CacheAction = "run"
)

// CacheQuery is what a cache decision is made from.
type CacheQuery struct {
	// Key is the job's cache key, empty for a command job.
	Key      JobKey
	Settings Settings
	// DefaultSamples is the task's default sample count, used when a measure submission leaves
	// Samples at zero.
	DefaultSamples uint32
	NoCache        bool
	// Saved holds saved results, in any order. Only those under Key count, so a store may pass
	// everything it has for the commit.
	Saved []Result
}

// CachePlan is what to do about the cache.
type CachePlan struct {
	Action CacheAction
	// Samples is how many benchmark samples a measure job takes: all that were asked for with
	// CacheRun, the difference with CacheTopUp, and none with CacheReuse. It is zero for check
	// and command jobs.
	Samples uint32
	// FreshCheck says a measure job that runs must run its own check and not link a saved pass.
	// It is the check job's own query that is answered with NoCache set.
	FreshCheck bool
	// From lists the jobs of the saved results the answer builds on, oldest first: the most
	// recent pass for a reused check and every measurement for a reused or topped-up measure.
	From []JobID
}

// PlanCache decides what a submission does about saved results.
//
//   - A command job is never cached: it always runs.
//   - A check reuses a pass. A failed check saves no result, so a saved check outcome is a pass,
//     and failures always rerun.
//   - A measure reuses its saved samples. When more samples are asked for than were saved, only the
//     difference runs, and when none were saved the whole count runs. The count asked for is the
//     settings' Samples, or the task's default when that is zero.
//   - NoCache runs the whole job, with a fresh check. Recheck runs a fresh check in a measure job
//     that runs; a measure that is answered from saved samples runs nothing, so it has no check
//     to redo.
//
// The cache is the answer to a submission. It never satisfies a request for the current result
// (see CurrentResultOf), which needs samples and a check taken after the last promotion.
//
// It fails with a *Failure of kind FailureInvalidInput for missing settings, settings that are not
// check, command or measure settings, or a measure job with no sample count to ask for.
func PlanCache(q CacheQuery) (CachePlan, error) {
	switch settings := q.Settings.(type) {
	case CommandSettings:
		return CachePlan{Action: CacheRun}, nil
	case CheckSettings:
		return planCheck(q), nil
	case MeasureSettings:
		return planMeasure(q, settings)
	case nil:
		return CachePlan{}, failf(FailureInvalidInput, "settings: missing")
	default:
		return CachePlan{}, failf(FailureInvalidInput, "settings: %T is not check, command or measure settings", q.Settings)
	}
}

func planCheck(q CacheQuery) CachePlan {
	passes := saved(q, func(r Result) bool { _, ok := r.Outcome.(CheckOutcome); return ok })
	if q.NoCache || len(passes) == 0 {
		return CachePlan{Action: CacheRun}
	}
	return CachePlan{Action: CacheReuse, From: []JobID{passes[len(passes)-1].Job}}
}

func planMeasure(q CacheQuery, settings MeasureSettings) (CachePlan, error) {
	want := settings.wanted(q.DefaultSamples)
	if want == 0 {
		return CachePlan{}, failf(FailureInvalidInput, "samples: a measure job needs a sample count, and neither the submission nor the task sets one")
	}
	if q.NoCache {
		return CachePlan{Action: CacheRun, Samples: want, FreshCheck: true}, nil
	}
	measurements := saved(q, func(r Result) bool { _, ok := r.Outcome.(MeasureOutcome); return ok })
	var have uint32
	from := make([]JobID, 0, len(measurements))
	for _, r := range measurements {
		have += uint32(len(r.Outcome.(MeasureOutcome).Samples))
		from = append(from, r.Job)
	}
	switch {
	case have >= want:
		return CachePlan{Action: CacheReuse, From: from}, nil
	case have == 0:
		return CachePlan{Action: CacheRun, Samples: want, FreshCheck: settings.Recheck}, nil
	default:
		return CachePlan{Action: CacheTopUp, Samples: want - have, FreshCheck: settings.Recheck, From: from}, nil
	}
}

// saved returns the results under the query's key that match, oldest first.
func saved(q CacheQuery, match func(Result) bool) []Result {
	var out []Result
	for _, r := range q.Saved {
		if r.Key == q.Key && match(r) {
			out = append(out, r)
		}
	}
	slices.SortFunc(out, func(a, b Result) int {
		if c := a.RecordedAt.Compare(b.RecordedAt); c != 0 {
			return c
		}
		return strings.Compare(string(a.Job), string(b.Job))
	})
	return out
}

// wanted is the sample count a measure submission asks for.
func (s MeasureSettings) wanted(taskDefault uint32) uint32 {
	if s.Samples == 0 {
		return taskDefault
	}
	return s.Samples
}

// CanJoin reports whether a submission may land on a job that is queued or running, rather than
// queue its own. Submitting the same work twice makes one job, but only when the existing job
// would answer the submission as well as a new one would:
//
//   - the existing job is not final, the submission does not set NoCache, and the two have one
//     non-empty key (a command job has none, so it never joins);
//   - a measure job also takes at least as many samples as the submission asks for, since a key
//     does not say how many samples a job takes, and carries a fresh check if the submission
//     rechecks.
//
// defaultSamples is the task's default sample count.
func CanJoin(existing Job, submission Submission, defaultSamples uint32) bool {
	if existing.State.Final() || submission.NoCache || existing.Key == "" || existing.Key != submission.Key {
		return false
	}
	switch theirs := submission.Spec.Settings.(type) {
	case CheckSettings:
		_, same := existing.Spec.Settings.(CheckSettings)
		return same
	case MeasureSettings:
		ours, same := existing.Spec.Settings.(MeasureSettings)
		return same && theirs.wanted(defaultSamples) > 0 &&
			ours.wanted(defaultSamples) >= theirs.wanted(defaultSamples) &&
			(ours.Recheck || !theirs.Recheck)
	}
	return false
}
