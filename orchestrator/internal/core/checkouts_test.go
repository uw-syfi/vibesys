package core_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// releases counts the ReleaseCheckouts of the last step.
func (w *script) releases() int {
	n := 0
	for _, k := range w.kinds() {
		if k == "ReleaseCheckouts" {
			n++
		}
	}
	return n
}

// Working copies are released when the run is over and at no earlier step: not while it is running,
// not while it is stopping with an agent still open, and once only. This is the live finding (12
// worktrees left in the user's repository) and the rule that nothing is removed under a run that can
// continue.
func TestCheckoutsAreReleasedExactlyWhenTheRunIsOver(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	worker := w.spawn(run, leadID, steers)
	if w.releases() != 0 {
		t.Fatalf("a run that is going released its checkouts: %v", w.kinds())
	}
	w.command(operator, core.StopRun{Run: run})
	if w.releases() != 0 {
		t.Fatalf("a stopping run released its checkouts: %v", w.kinds())
	}
	w.endTurn(worker, core.TurnInterrupted)
	w.fact(worker, core.SessionClosed{})
	if w.releases() != 0 {
		t.Fatalf("a run with its lead still open released its checkouts: %v", w.kinds())
	}
	w.fact(leadID, core.SessionClosed{})
	if got, _ := w.s.Run(run); got.Status != core.RunEnded || w.releases() != 1 {
		t.Fatalf("run %s released %d times: %v", got.Status, w.releases(), w.kinds())
	}
	// Whatever happens to the run afterwards asks for nothing more.
	w.step(core.TimerFired{Timer: core.ArmTimer{Kind: core.TimerRunDeadline, Run: run}})
	if w.releases() != 0 {
		t.Fatalf("a later input released again: %v", w.kinds())
	}
}

// The answer names the working copies kept, and is recorded once: for a run that is over, as the run's
// own fact; never for one that is not, and only for the run's own subagents.
func TestTheKeptCheckoutsAreRecordedOnlyForARunThatIsOverAndOnlyForItsSubagents(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	worker := w.spawn(run, leadID, steers)
	kept := []core.Checkout{{Agent: worker, Location: "/state/w/worktree"}}

	w.step(core.CheckoutsReleased{Run: run, Kept: kept})
	if got, _ := w.s.Run(run); len(got.Kept) != 0 || len(w.events) != 0 {
		t.Fatalf("a run that can continue recorded kept checkouts: %+v, events %v", got.Kept, w.events)
	}

	w.command(operator, core.StopRun{Run: run})
	w.endTurn(worker, core.TurnInterrupted)
	w.fact(worker, core.SessionClosed{})
	w.fact(leadID, core.SessionClosed{})

	stray := []core.Checkout{{Agent: leadID, Location: "/repo"}, {Agent: "agent-elsewhere", Location: "/x"}}
	w.step(core.CheckoutsReleased{Run: run, Kept: append(stray, kept...)})
	if got, _ := w.s.Run(run); !reflect.DeepEqual(got.Kept, kept) {
		t.Fatalf("kept = %+v, want only the worker's %+v", got.Kept, kept)
	}
	w.step(core.CheckoutsReleased{Run: run, Kept: kept})
	if len(w.events) != 0 {
		t.Fatalf("a repeat of the answer wrote %v", w.events)
	}
	w.step(core.CheckoutsReleased{Run: "run-nowhere", Kept: kept})
	if len(w.events) != 0 {
		t.Fatalf("an answer for no run wrote %v", w.events)
	}
}

// For any order of answers, stops and closes, the kept checkouts of a run are those of the last
// answer after it was over, sorted, naming only its subagents; and a replay of the log holds the same.
func TestKeptCheckoutsFollowTheLastAnswerOfARunThatIsOver(t *testing.T) {
	for seed := range uint64(200) {
		r := rand.New(rand.NewPCG(seed, 3))
		w, run, leadID := started(t, 4, steers)
		var workers []core.AgentID
		for range 1 + r.IntN(3) {
			workers = append(workers, w.spawn(run, leadID, steers))
		}
		var want []core.Checkout
		over := false
		for range 12 {
			var answer []core.Checkout
			for _, id := range workers {
				if r.IntN(2) == 0 {
					answer = append(answer, core.Checkout{Agent: id, Location: fmt.Sprintf("/state/%s/worktree", id)})
				}
			}
			r.Shuffle(len(answer), func(i, j int) { answer[i], answer[j] = answer[j], answer[i] })
			w.step(core.CheckoutsReleased{Run: run, Kept: answer})
			if over {
				want = sorted(answer)
			}
			if !over && r.IntN(3) == 0 {
				w.command(operator, core.StopRun{Run: run})
				for _, id := range workers {
					if w.status(id) == core.AgentActive {
						w.endTurn(id, core.TurnInterrupted)
					}
					if !over {
						w.fact(id, core.SessionClosed{})
					}
				}
				w.fact(leadID, core.SessionClosed{})
				over = true
			}
		}
		if got, _ := w.s.Run(run); !reflect.DeepEqual(got.Kept, want) && (len(got.Kept) != 0 || len(want) != 0) {
			t.Fatalf("seed %d: kept = %+v, want %+v", seed, got.Kept, want)
		}
		if again := core.Replay(core.State{}, w.log); !reflect.DeepEqual(again, w.s) {
			t.Fatalf("seed %d: a replay of the log differs from the state", seed)
		}
	}
}

func sorted(in []core.Checkout) []core.Checkout {
	out := append([]core.Checkout(nil), in...)
	for i := range out {
		for j := i + 1; j < len(out); j++ {
			if out[j].Agent < out[i].Agent {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}
