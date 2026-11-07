package core_test

import (
	"cmp"
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// These stories pin Catch me up one rule at a time. TestSimulation holds the same rules over random
// interleavings of everything that can happen to a run, against the monitor's own books.

var (
	ann = core.Operator{Viewer: core.Viewer{ID: "install-ann", Label: "Ann's laptop"}}
	bob = core.Operator{Viewer: core.Viewer{ID: "install-bob", Label: "Bob's desktop"}}
)

// press has a viewer press Catch me up on a run and returns the receipt and the check-in it names.
func (w *script) press(who core.Operator, run core.RunID) (core.Receipt, core.CheckIn) {
	w.t.Helper()
	r := w.command(who, core.RequestCatchUp{Run: run})
	if r.Refusal != "" {
		w.t.Fatalf("a catch-up was refused: %s", r.Refusal)
	}
	c, ok := w.s.CheckIn(r.CheckIn)
	if !ok {
		w.t.Fatalf("the receipt names check-in %q, which the state does not have", r.CheckIn)
	}
	return r, c
}

func (w *script) checkIn(id core.CheckInID) core.CheckIn {
	w.t.Helper()
	c, ok := w.s.CheckIn(id)
	if !ok {
		w.t.Fatalf("no check-in %s", id)
	}
	return c
}

// requestsTo counts the requests for a report that reached a lead, once each.
func (w *script) catchUpRequests() []core.CatchUpRequest {
	var out []core.CatchUpRequest
	seen := map[core.MessageID]bool{}
	for _, e := range w.log {
		if m, ok := e.Body.(core.Message); ok && !seen[m.ID] {
			if b, ok := m.Body.(core.CatchUpRequest); ok {
				seen[m.ID] = true
				out = append(out, b)
			}
		}
	}
	return out
}

func factsOf(spent uint32) core.WindowFacts {
	return core.WindowFacts{AgentsStarted: spent, PlansCreated: []uint32{1, 2}}
}

// A press records a check-in over the whole run so far, answers at once with its id, and asks the host to
// work the facts out. The facts freeze, and with them the lead is asked for a report in the same window,
// worded from those facts.
func TestAPressRecordsACheckInAndTheFrozenFactsAskTheLead(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	created := w.checkRun(run).CreatedAt
	r, c := w.press(ann, run)
	if r.Message != "" || r.Agent != "" || r.Run != run {
		t.Fatalf("receipt %+v", r)
	}
	if c.Run != run || c.Viewer != ann.Viewer || !c.Since.Equal(created) || !c.At.Equal(w.now) || c.Ask != "" || c.Report != "" {
		t.Fatalf("check-in %+v", c)
	}
	if _, pending := c.Outcome.(core.CheckInPending); !pending {
		t.Fatalf("a new check-in is %T", c.Outcome)
	}
	var computing []core.ComputeWindowFacts
	for _, e := range w.effects {
		if b, ok := e.(core.ComputeWindowFacts); ok {
			computing = append(computing, b)
		}
	}
	if len(computing) != 1 || computing[0] != (core.ComputeWindowFacts{CheckIn: c.ID, Run: run, Since: c.Since, Until: c.At}) {
		t.Fatalf("effects %v", w.kinds())
	}
	if asks := w.catchUpRequests(); len(asks) != 0 {
		t.Fatalf("the lead was asked before the facts were frozen: %+v", asks)
	}

	w.step(core.WindowFactsReady{CheckIn: c.ID, Facts: factsOf(1)})
	ready, ok := w.checkIn(c.ID).Outcome.(core.CheckInReady)
	if !ok || !equalFacts(ready.Facts, factsOf(1)) {
		t.Fatalf("outcome %+v", w.checkIn(c.ID).Outcome)
	}
	if asks := w.catchUpRequests(); len(asks) != 1 || asks[0] != (core.CatchUpRequest{CheckIn: c.ID, Since: c.Since, Until: c.At}) || w.checkIn(c.ID).Ask == "" {
		t.Fatalf("the lead was asked %+v, the check-in's ask is %q", asks, w.checkIn(c.ID).Ask)
	}
	if got := w.turn(leadID).Trigger; got != core.TriggerOperator {
		t.Fatalf("the request started a turn triggered by %s", got)
	}
	// The facts are frozen: no later answer changes them, whatever it says.
	w.step(core.WindowFactsReady{CheckIn: c.ID, Facts: factsOf(9)})
	w.step(core.WindowFactsFailed{CheckIn: c.ID, Detail: "late"})
	if len(w.events) != 0 || !equalFacts(w.checkIn(c.ID).Outcome.(core.CheckInReady).Facts, factsOf(1)) || len(w.catchUpRequests()) != 1 {
		t.Fatalf("a later answer changed a settled check-in: %+v", w.events)
	}
	w.step(core.WindowFactsReady{CheckIn: "no-such-check-in", Facts: factsOf(1)})
	if len(w.events) != 0 {
		t.Fatalf("an answer for no check-in changed %+v", w.events)
	}
}

// A check-in whose facts could not be worked out asks nobody: the viewer presses again, and that press asks.
func TestAFailedCheckInAsksNoOne(t *testing.T) {
	w, run, _ := started(t, 2, steers)
	_, c := w.press(ann, run)
	w.step(core.WindowFactsFailed{CheckIn: c.ID, Detail: "the job log cannot be read"})
	if w.checkIn(c.ID).Ask != "" || len(w.catchUpRequests()) != 0 {
		t.Fatalf("a failed check-in asked the lead: %+v", w.catchUpRequests())
	}
}

func equalFacts(a, b core.WindowFacts) bool {
	return a.AgentsStarted == b.AgentsStarted && slices.Equal(a.PlansCreated, b.PlansCreated)
}

func (w *script) checkRun(run core.RunID) core.Run {
	w.t.Helper()
	r, ok := w.s.Run(run)
	if !ok {
		w.t.Fatalf("no run %s", run)
	}
	return r
}

// A viewer's windows follow one another: the second starts where the first ended.
func TestAViewersWindowsFollowOneAnother(t *testing.T) {
	w, run, _ := started(t, 2, steers)
	_, first := w.press(ann, run)
	w.step(core.WindowFactsReady{CheckIn: first.ID, Facts: factsOf(1)})
	_, second := w.press(ann, run)
	if !second.Since.Equal(first.At) || !second.At.After(second.Since) {
		t.Fatalf("first %v to %v, second %v to %v", first.Since, first.At, second.Since, second.At)
	}
	// Another viewer has windows of their own, from the start of the run.
	_, other := w.press(bob, run)
	if !other.Since.Equal(w.checkRun(run).CreatedAt) {
		t.Fatalf("another viewer's first window starts at %v", other.Since)
	}
}

// A press while the viewer's last check-in is still being worked out answers with that one: no second
// window and no second effect, whatever command id it comes under.
func TestAPressWhilePendingAnswersWithTheSameCheckIn(t *testing.T) {
	w, run, _ := started(t, 2, steers)
	first, c := w.press(ann, run)
	second := w.command(ann, core.RequestCatchUp{Run: run})
	if second.CheckIn != c.ID || second.Message != first.Message || second.Refusal != "" {
		t.Fatalf("first %+v, second %+v", first, second)
	}
	if len(w.effects) != 0 || len(w.s.CheckIns(run)) != 1 || len(w.catchUpRequests()) != 0 {
		t.Fatalf("a repeated press made effects %v, check-ins %d, requests %d", w.kinds(), len(w.s.CheckIns(run)), len(w.catchUpRequests()))
	}
	// A different viewer is not held up by it.
	_, other := w.press(bob, run)
	if other.ID == c.ID {
		t.Fatal("another viewer was given the pending check-in")
	}
	// The same command repeated, as a retry is, gets the stored receipt.
	id := core.CommandID("retry")
	d := core.Command{ID: id, Digest: "d", Caller: ann, Body: core.RequestCatchUp{Run: run}}
	w.step(d)
	was, _ := w.s.Recall(id, "d")
	w.step(d)
	if again, _ := w.s.Recall(id, "d"); !reflect.DeepEqual(again, was) || len(w.events) != 0 {
		t.Fatalf("a retry changed %+v to %+v", was, again)
	}
}

// A check-in that failed does not move where the viewer's next window starts: no moment is skipped.
func TestAFailedCheckInDoesNotMoveTheNextWindow(t *testing.T) {
	w, run, _ := started(t, 2, steers)
	_, ok := w.press(ann, run)
	w.step(core.WindowFactsReady{CheckIn: ok.ID, Facts: factsOf(1)})
	_, bad := w.press(ann, run)
	w.step(core.WindowFactsFailed{CheckIn: bad.ID, Detail: "the job log cannot be read"})
	if got, _ := w.checkIn(bad.ID).Outcome.(core.CheckInFailed); got.Detail != "the job log cannot be read" {
		t.Fatalf("outcome %+v", w.checkIn(bad.ID).Outcome)
	}
	_, retry := w.press(ann, run)
	if !retry.Since.Equal(ok.At) || retry.ID == bad.ID {
		t.Fatalf("the retry starts at %v, want %v (the end of the last window that did not fail)", retry.Since, ok.At)
	}
	// A viewer whose only check-in failed starts again from the run's start.
	_, other := w.press(bob, run)
	w.step(core.WindowFactsFailed{CheckIn: other.ID, Detail: "x"})
	_, again := w.press(bob, run)
	if !again.Since.Equal(w.checkRun(run).CreatedAt) {
		t.Fatalf("starts at %v", again.Since)
	}
}

// A run that is over can be caught up on, and the window ends where the run did, so that what a later
// run does to the same repository is never in it.
func TestACatchUpOnARunThatIsOverEndsWhereTheRunEnded(t *testing.T) {
	w, run, _ := started(t, 2, steers)
	_, early := w.press(ann, run)
	w.step(core.WindowFactsReady{CheckIn: early.ID, Facts: factsOf(1)})
	w.command(operator, core.StopRun{Run: run})
	for w.checkRun(run).Status != core.RunEnded {
		stopAll(w, run)
	}
	ended := w.checkRun(run).EndedAt
	w.now = w.now.Add(48 * time.Hour)
	before := len(w.catchUpRequests())
	_, late := w.press(ann, run)
	if !late.Since.Equal(early.At) || !late.At.Equal(ended) || late.Ask != "" || len(w.catchUpRequests()) != before {
		t.Fatalf("early %v to %v, the run ended at %v, late %+v", early.Since, early.At, ended, late)
	}
	w.step(core.WindowFactsReady{CheckIn: late.ID, Facts: factsOf(2)})
	// Caught up: the next press has nothing to cover.
	_, empty := w.press(ann, run)
	if !empty.Since.Equal(ended) || !empty.At.Equal(ended) {
		t.Fatalf("after catching up the window is %v to %v", empty.Since, empty.At)
	}
	// And a viewer who never looked sees the whole run, up to its end.
	_, whole := w.press(bob, run)
	if !whole.Since.Equal(w.checkRun(run).CreatedAt) || !whole.At.Equal(ended) {
		t.Fatalf("a first look at a finished run covers %v to %v", whole.Since, whole.At)
	}
}

// stopAll lets every live agent of a run close, one step at a time.
func stopAll(w *script, run core.RunID) {
	w.t.Helper()
	for _, a := range w.s.Agents(run) {
		if a.Status.Over() {
			continue
		}
		if _, running := w.s.ActiveTurn(a.ID); running {
			w.endTurn(a.ID, core.TurnInterrupted)
			continue
		}
		w.fact(a.ID, core.SessionClosed{})
	}
}

// The request never steers into a turn that is running. It waits for the lead to rest and starts a
// turn of its own, so the lead is not interrupted, and the report posted in that turn is the answer.
func TestTheRequestStartsATurnOfItsOwnAndTheReportInItAnswers(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	// The lead is busy in a turn that something else started, and it can be steered.
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
	busy := w.turn(leadID)
	_, c := w.press(ann, run)
	w.step(core.WindowFactsReady{CheckIn: c.ID, Facts: factsOf(1)})
	c = w.checkIn(c.ID)
	r := core.Receipt{Message: c.Ask}
	if got := w.delivery(r.Message); r.Message == "" || got != core.DeliveryQueued {
		t.Fatalf("the request to a busy lead is %s, want queued: it must not steer into a turn", got)
	}
	// A report the lead posts in the turn it is in is not an answer to the request.
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "early"}})
	if w.checkIn(c.ID).Report != "" {
		t.Fatalf("a report posted in turn %s answered a request that had not been read", busy.ID)
	}
	w.endTurn(leadID, core.TurnCompleted)
	if got := w.turn(leadID); got.Message != r.Message || got.Trigger != core.TriggerOperator {
		t.Fatalf("the next turn is %+v, want the one the request starts", got)
	}
	if got := w.delivery(r.Message); got != core.DeliveryNewTurn {
		t.Fatalf("delivery %s", got)
	}
	answer := w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "since you looked"}})
	if got := w.checkIn(c.ID).Report; got != answer.Message || got == "" {
		t.Fatalf("the report %q was not linked: the check-in has %q", answer.Message, got)
	}
	// Only the first report of the turn is the answer.
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "again"}})
	if got := w.checkIn(c.ID).Report; got != answer.Message {
		t.Fatalf("a second report replaced the answer: %q", got)
	}
}

// No request is sent when the facts freeze for a run that cannot be asked, or one that has a request on
// its way, a viewer's or the schedule's; the check-in is made all the same, with its facts, and the reports
// in its window are the text.
func TestNoRequestIsSentWhenTheRunCannotBeAskedOrOneIsInFlight(t *testing.T) {
	t.Run("a viewer's already on its way", func(t *testing.T) {
		w, run, leadID := started(t, 2, steers)
		w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
		_, first := w.press(ann, run)
		w.step(core.WindowFactsReady{CheckIn: first.ID, Facts: factsOf(1)})
		_, second := w.press(ann, run)
		_, third := w.press(bob, run)
		w.step(core.WindowFactsReady{CheckIn: second.ID, Facts: factsOf(2)})
		w.step(core.WindowFactsReady{CheckIn: third.ID, Facts: factsOf(3)})
		if w.checkIn(first.ID).Ask == "" || w.checkIn(second.ID).Ask != "" || w.checkIn(third.ID).Ask != "" || len(w.catchUpRequests()) != 1 {
			t.Fatalf("asks %q %q %q, requests %d", w.checkIn(first.ID).Ask, w.checkIn(second.ID).Ask, w.checkIn(third.ID).Ask, len(w.catchUpRequests()))
		}
		if _, ready := w.checkIn(second.ID).Outcome.(core.CheckInReady); !ready || second.Since.Before(first.At) {
			t.Fatalf("second %+v", w.checkIn(second.ID))
		}
	})
	t.Run("the schedule's already on its way", func(t *testing.T) {
		w, run, leadID := scheduledRun(t, 20*time.Minute, 3, steers)
		w.spawnWorker(run, leadID)
		timer, ok := w.summaryTimer()
		if !ok {
			t.Fatal("work moved and armed no timer")
		}
		w.fire(timer)
		if len(w.requests()) != 1 {
			t.Fatalf("the schedule asked %+v", w.requests())
		}
		_, c := w.press(ann, run)
		w.step(core.WindowFactsReady{CheckIn: c.ID, Facts: factsOf(1)})
		if w.checkIn(c.ID).Ask != "" || len(w.catchUpRequests()) != 0 {
			t.Fatalf("the lead had a scheduled request on its way and was asked %+v too", w.catchUpRequests())
		}
		if _, ready := w.checkIn(c.ID).Outcome.(core.CheckInReady); !ready {
			t.Fatalf("outcome %+v", w.checkIn(c.ID).Outcome)
		}
	})
	t.Run("a viewer's on its way when the schedule falls due", func(t *testing.T) {
		w, run, leadID := scheduledRun(t, 20*time.Minute, 3, steers)
		w.spawnWorker(run, leadID)
		timer, ok := w.summaryTimer()
		if !ok {
			t.Fatal("work moved and armed no timer")
		}
		_, c := w.press(ann, run)
		w.step(core.WindowFactsReady{CheckIn: c.ID, Facts: factsOf(1)})
		if w.checkIn(c.ID).Ask == "" {
			t.Fatal("the lead was not asked")
		}
		w.fire(timer)
		if len(w.requests()) != 0 {
			t.Fatalf("the schedule sent %+v while a viewer's request was on its way", w.requests())
		}
	})
	t.Run("a lead that gave its final report", func(t *testing.T) {
		w, run, leadID := started(t, 2, steers)
		w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
		_, c := w.press(ann, run)
		w.step(core.WindowFactsReady{CheckIn: c.ID, Facts: factsOf(1)})
		if w.checkIn(c.ID).Ask != "" || len(w.catchUpRequests()) != 0 {
			t.Fatalf("a finished lead was asked: %+v", w.checkIn(c.ID))
		}
	})
	t.Run("a run that reached its cost limit", func(t *testing.T) {
		w := newScript(t)
		r := w.command(operator, core.StartRun{Repository: "repo", Task: "t", FanoutLimit: 1, Lead: lead, MaxCostUSD: 1, Brief: "go"})
		w.open(r.Agent, steers)
		spent := 2.0
		w.fact(r.Agent, core.TurnEnded{Turn: w.turn(r.Agent).ID, Status: core.TurnCompleted, Usage: core.Usage{CostUSD: &spent}})
		if w.checkRun(r.Run).LimitReached != core.LimitCost {
			t.Fatalf("run %+v", w.checkRun(r.Run))
		}
		_, c := w.press(ann, r.Run)
		w.step(core.WindowFactsReady{CheckIn: c.ID, Facts: factsOf(1)})
		if w.checkIn(c.ID).Ask != "" {
			t.Fatalf("a lead that was told to finish was asked: %+v", w.checkIn(c.ID))
		}
	})
}

// A catch-up needs a viewer, and a viewer is no authority: an agent may not press it, and the operator
// is the operator whoever is looking.
func TestACatchUpNeedsAViewerAndIsOnlyForTheOperator(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	if r := w.command(operator, core.RequestCatchUp{Run: run}); r.Refusal != core.RefuseNoViewer || r.CheckIn != "" || len(w.s.CheckIns(run)) != 0 {
		t.Fatalf("a press with no viewer: %+v", r)
	}
	half := core.Operator{Viewer: core.Viewer{ID: "install"}}
	if r := w.command(half, core.RequestCatchUp{Run: run}); r.Refusal != core.RefuseNoViewer {
		t.Fatalf("a press with no label: %+v", r)
	}
	if r := w.command(core.AgentCaller{Agent: leadID}, core.RequestCatchUp{Run: run}); r.Refusal != core.RefuseNotPermitted {
		t.Fatalf("a press by the lead: %+v", r)
	}
	if r := w.command(ann, core.RequestCatchUp{Run: "no-such-run"}); r.Refusal != core.RefuseRunNotFound {
		t.Fatalf("a press on no run: %+v", r)
	}
	// Whoever is looking may do what the operator does.
	if r := w.command(ann, core.SendMessage{Agent: leadID, Text: "hello"}); r.Refusal != "" {
		t.Fatalf("a viewer's message: %+v", r)
	}
	// The viewer is no part of what a command is: its digest is the caller's to compute, and a repeated
	// command is answered with its receipt whoever repeats it.
	w.step(core.Command{ID: "same", Digest: "d", Caller: ann, Body: core.SendMessage{Agent: leadID, Text: "again"}})
	w.step(core.Command{ID: "same", Digest: "d", Caller: bob, Body: core.SendMessage{Agent: leadID, Text: "again"}})
	if len(w.events) != 0 {
		t.Fatalf("another viewer's repeat was decided again: %v", w.events)
	}
}

// A window never ends before it starts, however the clock moved: a press the clock put before the
// viewer's last window is an empty window at that window's end.
func TestAClockThatWentBackMakesAnEmptyWindowAndNotABackwardOne(t *testing.T) {
	w, run, _ := started(t, 2, steers)
	_, first := w.press(ann, run)
	w.step(core.WindowFactsReady{CheckIn: first.ID, Facts: factsOf(1)})
	w.now = first.At.Add(-time.Hour)
	_, back := w.press(ann, run)
	if !back.Since.Equal(first.At) || back.At.Before(back.Since) {
		t.Fatalf("a window %v to %v after one that ended %v", back.Since, back.At, first.At)
	}
}

// Whatever the order of presses and answers, restarts included, every viewer's check-ins follow one
// another from the start of the run, only one is pending, and a replay of the log is the state.
func TestCheckInsOfEveryViewerTileTheRunWhateverTheOrder(t *testing.T) {
	viewers := []core.Operator{ann, bob, {Viewer: core.Viewer{ID: "install-cy", Label: "Cy"}}}
	for seed := range 200 {
		w := newScript(t)
		run, leadID := w.startRun("task", 2, steers)
		var pending []core.CheckInID
		for step := range 40 {
			switch (seed*7 + step*13 + step*step) % 6 {
			case 0, 1, 2:
				_, c := w.press(viewers[(seed+step)%len(viewers)], run)
				if _, isPending := c.Outcome.(core.CheckInPending); isPending && !slices.Contains(pending, c.ID) {
					pending = append(pending, c.ID)
				}
			case 3:
				if len(pending) > 0 {
					i := (seed + step) % len(pending)
					w.step(core.WindowFactsReady{CheckIn: pending[i], Facts: factsOf(uint32(step))})
					pending = slices.Delete(pending, i, i+1)
				}
			case 4:
				if len(pending) > 0 {
					i := (seed + step) % len(pending)
					w.step(core.WindowFactsFailed{CheckIn: pending[i], Detail: fmt.Sprint("failure ", step)})
					pending = slices.Delete(pending, i, i+1)
				}
			default:
				w.now = w.now.Add(time.Duration(1+step%7) * time.Minute)
				if _, running := w.s.ActiveTurn(leadID); running {
					w.endTurn(leadID, core.TurnCompleted)
				}
			}
			// A restart: the state is its log.
			if step%11 == 0 {
				if replayed := core.Replay(core.State{}, w.log); !replayed.Equal(w.s) {
					t.Fatalf("seed %d: a replay of the log differs from the state", seed)
				}
			}
		}
	}
}

// A run's check-ins are read from an index and not by scanning and sorting every check-in of every run, so
// the index must say what a scan would: the same check-ins, oldest first by the end of the window, then its
// start, then its id, whatever the order the presses and answers came in, and a replay of the log must
// build the same.
func TestTheIndexOfCheckInsSaysWhatAScanWould(t *testing.T) {
	viewers := []core.Operator{ann, bob, {Viewer: core.Viewer{ID: "install-cy", Label: "Cy"}}}
	for seed := range uint64(60) {
		draw := rand.New(rand.NewPCG(seed, 5))
		w := newScript(t)
		runs := []core.RunID{}
		for i := range 3 {
			run, _ := w.startRunIn(fmt.Sprint("repo-", i), "task", 2, steers)
			runs = append(runs, run)
		}
		var pending []core.CheckInID
		for step := range 45 {
			switch draw.IntN(5) {
			case 0, 1, 2:
				_, c := w.press(viewers[draw.IntN(len(viewers))], runs[draw.IntN(len(runs))])
				if _, isPending := c.Outcome.(core.CheckInPending); isPending && !slices.Contains(pending, c.ID) {
					pending = append(pending, c.ID)
				}
			case 3:
				if len(pending) > 0 {
					i := draw.IntN(len(pending))
					w.step(core.WindowFactsReady{CheckIn: pending[i], Facts: factsOf(uint32(step))})
					pending = slices.Delete(pending, i, i+1)
				}
			default:
				// Clocks go back as well as forward.
				w.now = w.now.Add(time.Duration(draw.IntN(9)-3) * time.Minute)
			}
		}
		latest := map[core.CheckInID]core.CheckIn{}
		for _, e := range w.log {
			if c, ok := e.Body.(core.CheckIn); ok {
				latest[c.ID] = c
			}
		}
		for _, state := range []core.State{w.s, core.Replay(core.State{}, w.log)} {
			for _, run := range runs {
				var want []core.CheckIn
				for _, c := range latest {
					if c.Run == run {
						want = append(want, c)
					}
				}
				slices.SortFunc(want, func(a, b core.CheckIn) int {
					return cmp.Or(a.At.Compare(b.At), a.Since.Compare(b.Since), cmp.Compare(a.ID, b.ID))
				})
				if got := state.CheckIns(run); !reflect.DeepEqual(got, want) {
					t.Fatalf("seed %d: the index of run %s says %+v, a scan says %+v", seed, run, got, want)
				}
			}
		}
	}
}
