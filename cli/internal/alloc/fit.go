package alloc

import (
	"slices"
	"time"
)

// FitResult is whether a job fits an allocation, with the durations that decided it.
type FitResult struct {
	Fits bool
	// Endless says the allocation has no end, so every job fits; TimeLeft is then zero.
	Endless bool
	// TimeLeft is EndsAt - now.
	TimeLeft time.Duration
	// Ahead is the longest the jobs ahead of it in the allocation may still run.
	Ahead time.Duration
}

// Fit says whether a job of the bound fits a started allocation now, behind the jobs it already
// holds: it fits iff Ahead + bound <= EndsAt - now - margin. Equal fits: a job whose bound is
// exactly the time left, less the margin and the work ahead, runs. An allocation with no end fits
// every job.
//
// Ahead is the queue's bounds, and for the running job what is left of its bound: all of it until
// the shell reported it started, and its start + bound - now after (never less than zero). A job
// never runs past its bound, so this never promises time the allocation does not have.
func Fit(l Live, bound time.Duration, now time.Time, margin time.Duration) FitResult {
	ahead := time.Duration(0)
	if c := l.Current; c != nil {
		left := c.Bound
		if !c.StartedAt.IsZero() {
			left = max(0, c.StartedAt.Add(c.Bound).Sub(now))
		}
		ahead += left
	}
	for _, q := range l.Queue {
		ahead += q.Bound
	}
	if l.EndsAt.IsZero() {
		return FitResult{Fits: true, Endless: true, Ahead: ahead}
	}
	left := l.EndsAt.Sub(now)
	return FitResult{Fits: ahead+bound <= left-margin, TimeLeft: left, Ahead: ahead}
}

// Choose is the allocation a job submitted to any allocation of its run goes to: a Running one of
// the run, on an eligible placement, that the job fits. Of those, the one where it starts soonest
// (least work ahead), then the one that ends soonest (so the longer ones stay free for longer
// jobs; one with no end is last), then the oldest. False when none fits: the job runs the way a
// job submitted without an allocation does.
func Choose(s State, cfg Config, j JobSubmitted, now time.Time) (ID, bool) {
	type option struct {
		id    ID
		ahead time.Duration
		ends  time.Time
		n     uint64
	}
	var options []option
	for _, a := range s.allocs {
		r, ok := a.Phase.(Running)
		if !ok || a.Run != j.Run || !slices.Contains(j.Eligible, a.Placement) {
			continue
		}
		f := Fit(r.Live, j.Bound, now, cfg.Placements[a.Placement].Margin)
		if !f.Fits {
			continue
		}
		n, _ := a.ID.number()
		options = append(options, option{id: a.ID, ahead: f.Ahead, ends: r.EndsAt, n: n})
	}
	if len(options) == 0 {
		return "", false
	}
	best := slices.MinFunc(options, func(x, y option) int {
		if x.ahead != y.ahead {
			return compare(x.ahead, y.ahead)
		}
		if !x.ends.Equal(y.ends) {
			switch {
			case x.ends.IsZero():
				return 1
			case y.ends.IsZero():
				return -1
			}
			return x.ends.Compare(y.ends)
		}
		return compare(x.n, y.n)
	})
	return best.id, true
}

// Declined is why Choose found no allocation for the job: of the run's allocations, the refusal that
// came closest to taking it (a fit that fell short, then an allocation of a placement the job may
// not use, then one that is not running), the oldest of equals; NoAllocation when the run holds none.
func Declined(s State, cfg Config, j JobSubmitted, now time.Time) Refusal {
	var best Refusal
	rank, bestN := -1, uint64(0)
	for _, a := range s.allocs {
		if a.Run != j.Run || a.Stage() == StageEnded {
			continue
		}
		var why Refusal
		var r int
		switch p, running := a.Phase.(Running); {
		case !running:
			why, r = notRunning(a), 0
		case !slices.Contains(j.Eligible, a.Placement):
			why, r = NotEligible{ID: a.ID, Placement: a.Placement}, 1
		default:
			f := Fit(p.Live, j.Bound, now, cfg.Placements[a.Placement].Margin)
			why, r = DoesNotFit{ID: a.ID, TimeLeft: f.TimeLeft, Ahead: f.Ahead, Bound: j.Bound, Margin: cfg.Placements[a.Placement].Margin}, 2
		}
		n, _ := a.ID.number()
		if r > rank || (r == rank && n < bestN) {
			best, rank, bestN = why, r, n
		}
	}
	if best == nil {
		return NoAllocation{Run: j.Run}
	}
	return best
}

func compare[T ~int64 | ~uint64](x, y T) int {
	switch {
	case x < y:
		return -1
	case x > y:
		return 1
	}
	return 0
}
