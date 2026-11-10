package alloc_test

import (
	"math/rand/v2"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// The fit is the rule as written, for any work ahead, bound, margin and time: Ahead + bound <=
// EndsAt - now - margin; with no end, every job fits.
func TestTheFitIsTheRuleForAnyInput(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	dur := func(n time.Duration) time.Duration { return time.Duration(r.Int64N(int64(n))) }
	for range 20000 {
		started := epoch.Add(dur(time.Hour))
		l := alloc.Live{StartedAt: started, EndsAt: started.Add(dur(24 * time.Hour))}
		if r.IntN(10) == 0 {
			l.EndsAt = time.Time{}
		}
		now := started.Add(dur(30 * time.Hour))
		var ahead time.Duration
		if r.IntN(2) == 0 {
			c := alloc.Slot{Job: "c", Bound: dur(6*time.Hour) + 1}
			left := c.Bound
			if r.IntN(2) == 0 {
				c.StartedAt = started.Add(dur(now.Sub(started) + 1))
				left = max(0, c.StartedAt.Add(c.Bound).Sub(now))
			}
			l.Current = &c
			ahead += left
		}
		for range r.IntN(4) {
			q := alloc.Slot{Job: "q", Bound: dur(3*time.Hour) + 1}
			l.Queue = append(l.Queue, q)
			ahead += q.Bound
		}
		bound, margin := dur(10*time.Hour)+1, dur(10*time.Minute)
		got := alloc.Fit(l, bound, now, margin)
		want := alloc.FitResult{Fits: true, Endless: true, Ahead: ahead}
		if !l.EndsAt.IsZero() {
			want = alloc.FitResult{Fits: ahead+bound <= l.EndsAt.Sub(now)-margin, TimeLeft: l.EndsAt.Sub(now), Ahead: ahead}
		}
		if got != want {
			t.Fatalf("Fit(%+v, %s, %s, %s) = %+v, want %+v", l, bound, now.Sub(epoch), margin, got, want)
		}
		// A longer bound, a later now or a wider margin never fits where a shorter one does not.
		if !got.Fits && (alloc.Fit(l, bound+time.Minute, now, margin).Fits || alloc.Fit(l, bound, now, margin+time.Minute).Fits) {
			t.Fatalf("the fit is not monotone at %+v", l)
		}
	}
}

// A job of any goes to the running allocation of its run, on an eligible placement, where it starts
// soonest, then the one that ends soonest (one with no end last), then the oldest; never to one of
// another run, one that waits or drains, or one it does not fit.
func TestAJobOfAnyGoesWhereItStartsSoonestThenToTheFirstToEnd(t *testing.T) {
	cfg := alloc.Config{Placements: map[string]alloc.Limits{
		gpu:   {MaxHeld: 8, MaxFor: 24 * time.Hour},
		local: {MaxHeld: 8, MaxFor: 24 * time.Hour, Endless: true},
	}}
	w := newWorld(t)
	w.cfg = cfg
	long := w.running(gpu, 12*time.Hour)   // a1
	short := w.running(gpu, 6*time.Hour)   // a2
	endless := w.running(local, time.Hour) // a3
	twin := w.running(gpu, 6*time.Hour)    // a4, the same end as a2
	busy := w.running(gpu, 3*time.Hour)    // a5, which ends first, but has work ahead
	other := w.askFor("run-b", gpu, 2*time.Hour)
	w.took(other)
	w.start(other) // a6, another run's
	w.submit("ahead", 30*time.Minute, busy)
	w.ask(gpu, time.Hour) // a7, waiting
	pick := func(bound time.Duration, eligible ...string) (alloc.ID, bool) {
		return alloc.Choose(w.s, w.cfg, alloc.JobSubmitted{Run: "run-a", Job: "j", Bound: bound, Eligible: eligible}, w.now)
	}
	for _, c := range []struct {
		bound    time.Duration
		eligible []string
		want     alloc.ID
	}{
		{time.Hour, []string{gpu, local}, short},        // a2 and a4 end together; a2 is older.
		{7 * time.Hour, []string{gpu, local}, long},     // only a1 and a3 fit; a3 has no end.
		{13 * time.Hour, []string{gpu, local}, endless}, // only the one with no end fits.
		{13 * time.Hour, []string{gpu}, ""},             // none fits.
		{time.Hour, []string{local}, endless},           // only one placement serves it.
		{2*time.Hour + 30*time.Minute, []string{}, ""},  // nothing serves it.
	} {
		got, ok := pick(c.bound, c.eligible...)
		if got != c.want || ok != (c.want != "") {
			t.Errorf("a job of %s on %v went to %q, want %q", c.bound, c.eligible, got, c.want)
		}
	}
	_ = twin
}

func equalStates(a, b alloc.State) bool { return reflect.DeepEqual(a, b) }

func equalActions(a, b []alloc.Action) bool { return reflect.DeepEqual(a, b) }
