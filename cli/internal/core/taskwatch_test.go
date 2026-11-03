package core_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// client is a reader of the facts a stream sends: a reset replaces what it holds, a change
// replaces the facts with the same keys.
type client map[core.FactKey]any

func (c *client) apply(s core.Send) {
	if s.Reset {
		*c = client{}
	}
	if *c == nil {
		*c = client{}
	}
	for _, f := range s.Facts {
		(*c)[f.Key] = f.Value
	}
}

// randomView is a view over a small universe of keys, so successive views overlap, lose keys and
// gain them.
func randomView(r *rand.Rand) core.TaskView {
	v := core.TaskView{}
	if r.IntN(8) == 0 {
		v.Health = core.StoreHealth{Away: core.AwayUnavailable, Failure: core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprint(r.IntN(2))}}
		return v
	}
	c := &core.TaskContent{Head: core.NoCurrentResult{Reason: core.NoHead, Head: commitN(r.IntN(3))}}
	for n := 1; n <= r.IntN(4); n++ {
		c.Plans = append(c.Plans, core.Plan{Number: core.PlanNumber(n), Title: fmt.Sprint(r.IntN(2))})
	}
	for n := 1; n <= r.IntN(3); n++ {
		c.Promotions = append(c.Promotions, core.PromotionRecord{Seq: uint64(n), Promotion: core.Promotion{Commit: commitN(n)}})
	}
	for n := 1; n <= r.IntN(8); n++ {
		state := []core.JobState{core.JobQueued, core.JobRunning, core.JobSucceeded}[r.IntN(3)]
		c.Jobs = append(c.Jobs, core.Job{ID: core.JobID(fmt.Sprintf("j%d", n)), State: state})
	}
	for n := 1; n <= r.IntN(3); n++ {
		c.Judgments = append(c.Judgments, core.Judgment{Commit: commitN(10 + n), Against: commitN(r.IntN(3))})
	}
	for n := 1; n <= r.IntN(2); n++ {
		c.Troubles = append(c.Troubles, core.JobTrouble{Job: core.JobID(fmt.Sprintf("j%d", n)), Failure: core.Failure{Message: fmt.Sprint(r.IntN(2))}})
	}
	v.Content = c
	return v
}

func keysOf(facts []core.Fact) map[core.FactKey]any {
	out := map[core.FactKey]any{}
	for _, f := range facts {
		out[f.Key] = f.Value
	}
	return out
}

// Whatever a task does between two looks, a reader that applies what the stream sends holds the
// task as the last look found it, apart from judgments that left the judged window, which it still
// holds and which name the head they were made against.
func TestAReaderOfTheSendsHoldsTheLastView(t *testing.T) {
	for seed := range uint64(properties) {
		r := newRand(seed)
		var sent core.Sent
		var held client
		for step := 0; step < 12; step++ {
			v := randomView(r)
			var s core.Send
			s, sent = core.PlanSend(sent, v)
			held.apply(s)
			if len(s.Facts) > core.MaxFactsPerMessage && !s.Reset {
				t.Fatalf("seed %d step %d: a change of %d facts is not a reset", seed, step, len(s.Facts))
			}
			// Applying a send twice is applying it once.
			again := client{}
			for k, val := range held {
				again[k] = val
			}
			again.apply(s)
			if s.Reset {
				// A reset replaces: applying it to its own result is the same.
				if !reflect.DeepEqual(map[core.FactKey]any(again), map[core.FactKey]any(held)) {
					t.Fatalf("seed %d step %d: applying a reset twice differs from once", seed, step)
				}
			}
			want := keysOf(v.Facts())
			if v.Content == nil {
				// Only the health is said; the rest is as it was.
				if !reflect.DeepEqual(held["health"], v.Health) {
					t.Fatalf("seed %d step %d: health %v, want %v", seed, step, held["health"], v.Health)
				}
				continue
			}
			for k, val := range want {
				if !reflect.DeepEqual(held[k], val) {
					t.Fatalf("seed %d step %d: key %s holds %v, want %v", seed, step, k, held[k], val)
				}
			}
			for k := range held {
				if _, ok := want[k]; !ok && k[:len("judgment/")] != "judgment/" {
					t.Fatalf("seed %d step %d: key %s is held and no longer in the task, with no reset", seed, step, k)
				}
			}
			// What was just sent is not sent again.
			if next, _ := core.PlanSend(sent, v); !next.Empty() {
				t.Fatalf("seed %d step %d: the same view sent again says %+v", seed, step, next)
			}
		}
	}
}

func TestTheFirstSendIsAResetOfTheWholeView(t *testing.T) {
	v := randomView(newRand(3))
	s, _ := core.PlanSend(nil, v)
	if !s.Reset || len(s.Facts) != len(v.Facts()) {
		t.Fatalf("first send = reset %v with %d facts, want a reset of %d", s.Reset, len(s.Facts), len(v.Facts()))
	}
}

func jobsView(n int, state core.JobState) core.TaskView {
	c := &core.TaskContent{Head: core.NoCurrentResult{Reason: core.NoHead}}
	for i := 1; i <= n; i++ {
		c.Jobs = append(c.Jobs, core.Job{ID: core.JobID(fmt.Sprintf("j%d", i)), State: state})
	}
	return core.TaskView{Content: c}
}

func TestAKeyThatIsGoneIsAReset(t *testing.T) {
	_, sent := core.PlanSend(nil, jobsView(3, core.JobRunning))
	s, _ := core.PlanSend(sent, jobsView(2, core.JobRunning))
	if !s.Reset {
		t.Fatalf("a task that lost a job sends %+v, want a reset", s)
	}
}

func TestAChangeOfMoreThanOneMessageIsAReset(t *testing.T) {
	_, sent := core.PlanSend(nil, jobsView(core.MaxFactsPerMessage+3, core.JobRunning))
	s, _ := core.PlanSend(sent, jobsView(core.MaxFactsPerMessage+3, core.JobSucceeded))
	if !s.Reset {
		t.Fatalf("a change of %d facts sends %d facts without a reset", core.MaxFactsPerMessage+3, len(s.Facts))
	}
	_, sent = core.PlanSend(nil, jobsView(core.MaxFactsPerMessage, core.JobRunning))
	s, _ = core.PlanSend(sent, jobsView(core.MaxFactsPerMessage, core.JobSucceeded))
	if s.Reset || len(s.Facts) != core.MaxFactsPerMessage {
		t.Fatalf("a change of exactly %d facts sends reset %v with %d facts, want them as a change", core.MaxFactsPerMessage, s.Reset, len(s.Facts))
	}
}

func TestAJudgmentThatLeavesTheWindowIsNotAReset(t *testing.T) {
	with := jobsView(1, core.JobRunning)
	with.Content.Judgments = []core.Judgment{{Commit: commitN(1), Against: commitN(0)}, {Commit: commitN(2), Against: commitN(0)}}
	without := jobsView(1, core.JobRunning)
	without.Content.Judgments = []core.Judgment{{Commit: commitN(2), Against: commitN(0)}}
	_, sent := core.PlanSend(nil, with)
	s, _ := core.PlanSend(sent, without)
	if !s.Empty() {
		t.Fatalf("a judgment that left the window sends %+v, want nothing", s)
	}
}

func TestAViewThatCouldNotBeReadChangesOnlyTheHealth(t *testing.T) {
	_, sent := core.PlanSend(nil, jobsView(2, core.JobRunning))
	away := core.TaskView{Health: core.StoreHealth{Away: core.AwayOwnedElsewhere}}
	s, sent2 := core.PlanSend(sent, away)
	if s.Reset || len(s.Facts) != 1 || s.Facts[0].Key != "health" {
		t.Fatalf("an unreadable store sends %+v, want only the health", s)
	}
	if again, _ := core.PlanSend(sent2, away); !again.Empty() {
		t.Fatalf("the same health sent again says %+v", again)
	}
	back, _ := core.PlanSend(sent2, jobsView(2, core.JobRunning))
	if back.Reset || len(back.Facts) != 1 || back.Facts[0].Key != "health" {
		t.Fatalf("a store that is back, unchanged, sends %+v, want only the health", back)
	}
}

func TestJudgedCommitsAreTheDefaultWhenUnsetAndRefusedWhenTooMany(t *testing.T) {
	for _, tc := range []struct {
		asked uint32
		want  int
		bad   bool
	}{{0, core.DefaultJudgedCommits, false}, {1, 1, false}, {100, 100, false}, {101, 0, true}, {1 << 31, 0, true}} {
		got, err := core.ResolveJudgedCommits(tc.asked)
		var f *core.Failure
		if tc.bad != (err != nil) || got != tc.want || (tc.bad && (!asFailure(err, &f) || f.Kind != core.FailureInvalidInput)) {
			t.Errorf("ResolveJudgedCommits(%d) = %d, %v; want %d, invalid input %v", tc.asked, got, err, tc.want, tc.bad)
		}
	}
}

func asFailure(err error, f **core.Failure) bool {
	got, ok := err.(*core.Failure)
	*f = got
	return ok
}

func TestTheNewestMeasuredCommitsAreDistinctNewestFirstAndAtMostN(t *testing.T) {
	for seed := range uint64(properties) {
		r := newRand(seed)
		var rs []core.Result
		for i := 0; i < r.IntN(12); i++ {
			var o core.Outcome = core.MeasureOutcome{}
			if r.IntN(4) == 0 {
				o = core.CheckOutcome{}
			}
			rs = append(rs, core.Result{Job: core.JobID(fmt.Sprintf("j%d", i)), Commit: commitN(r.IntN(5)), RecordedAt: at(r.IntN(30)), Outcome: o})
		}
		n := 1 + r.IntN(6)
		got := core.NewestMeasured(rs, n)
		if len(got) > n {
			t.Fatalf("seed %d: %d commits for n=%d", seed, len(got), n)
		}
		seen := map[core.Commit]bool{}
		for _, c := range got {
			if seen[c] {
				t.Fatalf("seed %d: %s twice", seed, c)
			}
			seen[c] = true
		}
		newest := func(c core.Commit) (latest int, any bool) {
			for _, res := range rs {
				if _, m := res.Outcome.(core.MeasureOutcome); m && res.Commit == c {
					if mins := int(res.RecordedAt.Sub(epoch).Minutes()); !any || mins > latest {
						latest, any = mins, true
					}
				}
			}
			return
		}
		for i := 1; i < len(got); i++ {
			a, _ := newest(got[i-1])
			b, _ := newest(got[i])
			if a < b {
				t.Fatalf("seed %d: %s is listed before %s, which was measured later", seed, got[i-1], got[i])
			}
		}
	}
}
