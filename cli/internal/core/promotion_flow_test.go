package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func hex40(n int) core.Commit { return core.Commit(strings.Repeat(fmt.Sprintf("%x", n%15+1), 40)) }

// flowWorld is what a promotion changes: the branch head and the record, and nothing else. A step
// is applied to it the way the daemon applies it to git and to the store.
type flowWorld struct {
	initial    core.Commit
	head       core.Commit
	promotions []core.Promotion
}

// effects counts the effects done, so a test can crash at the n-th.
type fault struct {
	at    int  // the index of the effect that is cut off, from 0
	after bool // the effect took place and its caller never heard: the crash was just after it
}

func (w *flowWorld) apply(step core.PromotionStep) {
	switch s := step.(type) {
	case core.RecordPromotionStep:
		w.promotions = append(w.promotions, s.Promotion)
	case core.MoveBranchStep:
		if w.head != s.From {
			panic(fmt.Sprintf("a move from %s while the branch is at %s", s.From, w.head))
		}
		w.head = s.To
	}
}

func isEffect(step core.PromotionStep) bool {
	switch step.(type) {
	case core.RecordPromotionStep, core.MoveBranchStep:
		return true
	}
	return false
}

// run carries the promotion out until it is done, or until the fault cuts it off, and reports
// whether it was cut off. count is the number of effects tried so far across runs.
func (w *flowWorld) run(p core.Promotion, f *fault, count *int) (crashed bool) {
	for range 8 {
		step := core.NextPromotionStep(p, w.head, w.promotions)
		if !isEffect(step) {
			if _, diverged := step.(core.PromotionDivergedStep); diverged {
				panic("diverged in a world only the flow changes")
			}
			return false
		}
		i := *count
		*count++
		if f != nil && f.at == i {
			if f.after {
				w.apply(step)
			}
			return true
		}
		w.apply(step)
	}
	panic("a promotion did not settle in 8 steps")
}

// settle is what the daemon does at its start and on every look at the branch: carry out the last
// recorded promotion, if the branch has not caught up with it.
func (w *flowWorld) settle() {
	if len(w.promotions) == 0 {
		return
	}
	last := w.promotions[len(w.promotions)-1]
	var count int
	if w.run(last, nil, &count) || count > 1 {
		panic("settling did more than the one move")
	}
}

func (w *flowWorld) check(t *testing.T, context string) {
	t.Helper()
	known := map[core.Commit]bool{w.initial: true}
	for _, p := range w.promotions {
		known[p.Commit] = true
	}
	if !known[w.head] {
		t.Fatalf("%s: the branch is at %.7s, ahead of the record %v: the record must come first", context, w.head, w.promotions)
	}
	previous := w.initial
	for _, p := range w.promotions {
		if p.PreviousHead != previous {
			t.Fatalf("%s: the record does not chain: %v", context, w.promotions)
		}
		previous = p.Commit
	}
}

func promotionOf(previous, commit core.Commit, n int) core.Promotion {
	return core.Promotion{Commit: commit, PreviousHead: previous, At: time.Unix(int64(1000+n), 0).UTC()}
}

// A promotion cut off at any one of its effects, before or after it took place, leaves a state
// the branch is never ahead of the record in, and settling completes it. Several promotions in a
// row, with a cut at each possible effect, cover every ordering of crash and recovery.
func TestAPromotionCutOffAtAnyEffectIsCompletedBySettling(t *testing.T) {
	const promotions = 3
	totalEffects := 2 * promotions
	for at := range totalEffects {
		for _, after := range []bool{false, true} {
			name := fmt.Sprintf("effect %d %s", at, map[bool]string{false: "cut before", true: "cut after"}[after])
			t.Run(name, func(t *testing.T) {
				w := &flowWorld{initial: hex40(0), head: hex40(0)}
				f := &fault{at: at, after: after}
				var count int
				for n := 1; n <= promotions; n++ {
					w.settle()
					w.check(t, "after settling")
					if len(w.promotions) > 0 && w.head != w.promotions[len(w.promotions)-1].Commit {
						t.Fatalf("settled head %.7s is not the last promoted commit", w.head)
					}
					p := promotionOf(w.head, hex40(n), n)
					crashed := w.run(p, f, &count)
					w.check(t, "after the promotion")
					if crashed {
						// The process died: only the state is left. A cut before the record leaves no trace,
						// and every other cut leaves a record that settling finishes.
						w.settle()
						w.check(t, "after recovery")
						if recorded := slices.ContainsFunc(w.promotions, func(q core.Promotion) bool { return q.Commit == p.Commit }); recorded && w.head != p.Commit {
							t.Fatalf("recorded, but the branch is at %.7s after recovery", w.head)
						} else if !recorded && w.head != p.PreviousHead {
							t.Fatalf("not recorded, but the branch moved to %.7s", w.head)
						}
						f = nil
					}
				}
				w.settle()
				w.check(t, "at the end")
			})
		}
	}
}

// With no fault, a promotion is exactly two effects, record then move, and then it is done; asking
// again changes nothing, so a retry of a finished promotion is harmless.
func TestAPromotionIsRecordThenMoveThenDone(t *testing.T) {
	p := promotionOf(hex40(0), hex40(1), 1)
	w := &flowWorld{initial: hex40(0), head: hex40(0)}
	var steps []string
	for range 5 {
		step := core.NextPromotionStep(p, w.head, w.promotions)
		steps = append(steps, fmt.Sprintf("%T", step))
		if !isEffect(step) {
			break
		}
		w.apply(step)
	}
	want := []string{"core.RecordPromotionStep", "core.MoveBranchStep", "core.PromotionDoneStep"}
	if !slices.Equal(steps, want) {
		t.Fatalf("steps = %v, want %v", steps, want)
	}
	if _, done := core.NextPromotionStep(p, w.head, w.promotions).(core.PromotionDoneStep); !done {
		t.Fatal("asking again after it is done must still say done")
	}
}

// A branch somewhere other than where the promotion left it, or at the promoted commit before
// anything was recorded, was moved by someone else: whatever else is true, the answer is
// diverged, which names no effect, so nothing is written over their move.
func TestABranchMovedByAnotherIsNeverWrittenOver(t *testing.T) {
	rng := rand.New(rand.NewPCG(3, 5))
	for range 500 {
		previous, commit := hex40(rng.IntN(15)), hex40(rng.IntN(15))
		p := promotionOf(previous, commit, rng.IntN(100))
		head := hex40(rng.IntN(15))
		var record []core.Promotion
		recorded := rng.IntN(2) == 0
		if recorded {
			record = append(record, p)
		}
		step := core.NextPromotionStep(p, head, record)
		_, isDiverged := step.(core.PromotionDivergedStep)
		switch {
		case !recorded && head == previous:
			if _, ok := step.(core.RecordPromotionStep); !ok {
				t.Fatalf("unrecorded at the previous head: %T", step)
			}
		case recorded && head == previous:
			if _, ok := step.(core.MoveBranchStep); !ok {
				t.Fatalf("recorded at the previous head: %T", step)
			}
		case recorded && head == commit:
			if _, ok := step.(core.PromotionDoneStep); !ok {
				t.Fatalf("recorded at the commit: %T", step)
			}
		default:
			if !isDiverged {
				t.Fatalf("head %.7s, previous %.7s, commit %.7s, recorded %v: %T, want diverged", head, previous, commit, recorded, step)
			}
		}
	}
}
