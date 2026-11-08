package core_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// What the lead is told of the jobs of its run depends on when the job feed is read and when the run
// ends, and on nothing else. The feed is read on a timer, so a job that settles and a lead that
// finishes between two reads leave the run with no notice of it, and a run whose whole work fits between
// two reads is told of none. These tests state the rule that holds however the reads fall.

// leadNotices returns the jobs the lead was told of, in the order it was told, from every message
// the script has written to it.
func (w *script) leadNotices(lead core.AgentID) []core.JobID {
	var out []core.JobID
	seen := map[core.MessageID]bool{}
	for _, e := range w.log {
		m, ok := e.Body.(core.Message)
		if !ok || m.To != lead || seen[m.ID] {
			continue
		}
		if b, ok := m.Body.(core.JobFinished); ok {
			seen[m.ID] = true
			out = append(out, b.Job)
		}
	}
	return out
}

// pollJobs is one read of the feed: every job's state, and the count going, as the daemon applies them.
func (w *script) pollJobs(run core.RunID, states map[core.JobID]core.JobState, order []core.JobID) {
	var going uint32
	for _, id := range order {
		w.step(core.JobUpdate{Run: run, Job: id, State: states[id]})
		if !states[id].Final() {
			going++
		}
	}
	w.step(core.JobsRunning{Run: run, Count: going})
}

// A job is told to the lead exactly when a read finds it settled while the run is live: once however
// often later reads repeat it, in the order the reads found them, and never after the run has stopped.
// Reads that fall before the jobs settle, or after the run, tell the lead nothing, which is why the number
// of notices of a whole run is not a fact of the run.
func TestTheLeadIsToldOfEachJobFoundSettledWhileTheRunIsLiveExactlyOnce(t *testing.T) {
	for seed := range uint64(300) {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			r := rand.New(rand.NewPCG(seed, 7))
			w, run, leadID := started(t, 1, steers)
			n := 1 + r.IntN(6)
			var order []core.JobID
			states := map[core.JobID]core.JobState{}
			for i := range n {
				id := core.JobID(fmt.Sprintf("j%d", i))
				order = append(order, id)
				states[id] = core.JobQueued
			}
			var want []core.JobID // what a read found settled while the run was live, in order
			told := map[core.JobID]bool{}
			finish := r.IntN(4 * n) // the step at which the lead gives its final report
			for step := range 4 * n {
				// Jobs advance on their own, between reads.
				for _, id := range order {
					switch {
					case states[id] == core.JobQueued && r.IntN(3) == 0:
						states[id] = core.JobRunning
					case states[id] == core.JobRunning && r.IntN(3) == 0:
						states[id] = []core.JobState{core.JobSucceeded, core.JobFailed, core.JobCancelled}[r.IntN(3)]
					}
				}
				if step == finish {
					if _, ok := w.s.ActiveTurn(leadID); !ok {
						w.command(operator, core.SendMessage{Agent: leadID, Text: "finish"})
					}
					w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
				}
				if r.IntN(2) == 0 { // a read of the feed
					live := w.runOf(run).Status.Live()
					w.pollJobs(run, states, order)
					for _, id := range order {
						if live && states[id].Final() && !told[id] {
							told[id] = true
							want = append(want, id)
						}
					}
				}
				// The lead works through what it was told.
				for {
					if _, ok := w.s.ActiveTurn(leadID); !ok {
						break
					}
					w.endTurn(leadID, core.TurnCompleted)
				}
			}
			if got := w.leadNotices(leadID); !slices.Equal(got, want) {
				t.Fatalf("the lead was told of %v, want %v (jobs now %v)", got, want, states)
			}
		})
	}
}

// A run whose jobs all settle and whose lead gives its final report between two reads of the feed ends
// having told the lead of none of them: the run is correct, and a count of notices cannot be asked of it.
func TestARunThatEndsBetweenTwoReadsOfTheFeedTellsTheLeadOfNoJob(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	w.pollJobs(run, map[core.JobID]core.JobState{}, nil) // the first read finds nothing yet
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go"})
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
	w.endTurn(leadID, core.TurnCompleted)
	w.fact(leadID, core.SessionClosed{})
	if got := w.runOf(run); got.Status != core.RunEnded {
		t.Fatalf("the run is %s, want ended", got.Status)
	}
	w.pollJobs(run, map[core.JobID]core.JobState{"a": core.JobSucceeded, "b": core.JobFailed}, []core.JobID{"a", "b"})
	if got := w.leadNotices(leadID); len(got) != 0 {
		t.Fatalf("an ended run told its lead of %v", got)
	}
}
