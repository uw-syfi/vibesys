package core_test

import (
	"math/rand/v2"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// These stories pin the report schedule one rule at a time. The simulation (see TestSimulation and
// TestTheReportScheduleHoldsInRunsThatPauseAndReport) holds the same rules over random interleavings
// of everything that can happen to a run, against the monitor's own books.

// scheduledRun starts a run that asks its lead for a progress report every `every` (the default when
// zero), opens the lead with caps and lets it rest after its brief.
func scheduledRun(t *testing.T, every time.Duration, fanout uint32, caps core.Capabilities) (*script, core.RunID, core.AgentID) {
	t.Helper()
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: fanout, SummaryEvery: every, Lead: lead, Brief: "go"})
	w.open(r.Agent, caps)
	w.endTurn(r.Agent, core.TurnCompleted)
	return w, r.Run, r.Agent
}

// summaryTimer is the timer the last step armed for a request coming due, if it armed one.
func (w *script) summaryTimer() (core.ArmTimer, bool) {
	for _, e := range w.effects {
		if t, ok := e.(core.ArmTimer); ok && t.Kind == core.TimerSummaryDue {
			return t, true
		}
	}
	return core.ArmTimer{}, false
}

// requests are the requests for a report the script has written, once each, in the order written.
func (w *script) requests() []core.ReportDue {
	var out []core.ReportDue
	seen := map[core.MessageID]bool{}
	for _, e := range w.log {
		if m, ok := e.Body.(core.Message); ok && !seen[m.ID] {
			if b, ok := m.Body.(core.ReportDue); ok {
				seen[m.ID] = true
				out = append(out, b)
			}
		}
	}
	return out
}

// post has the lead post a report of a kind.
func (w *script) post(lead core.AgentID, kind core.ReportKind) {
	w.t.Helper()
	if r := w.command(core.AgentCaller{Agent: lead}, core.PostReport{Report: core.Report{Kind: kind, Headline: "h"}}); r.Refusal != "" {
		w.t.Fatalf("a %s report was refused: %s", kind, r.Refusal)
	}
}

// A run keeps the interval it was started with, the default when it named none, counts it from its
// start, and has nothing to ask before work moves.
func TestARunKeepsItsIntervalAndAsksNothingBeforeWorkMoves(t *testing.T) {
	for _, tc := range []struct{ asked, want time.Duration }{
		{0, core.DefaultSummaryEvery}, {5 * time.Minute, 5 * time.Minute}, {90 * time.Minute, 90 * time.Minute}, {36 * time.Hour, 36 * time.Hour},
	} {
		w := newScript(t)
		r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: 1, SummaryEvery: tc.asked, Lead: lead})
		if _, armed := w.summaryTimer(); armed {
			t.Fatalf("a start armed a timer for a report, with no work done")
		}
		got := w.runOf(r.Run)
		if got.SummaryEvery != tc.want || !got.LastSummaryAt.Equal(got.CreatedAt) || !got.NextSummaryAt.IsZero() {
			t.Fatalf("asked for %v, the run is %+v", tc.asked, got)
		}
	}
}

// The default is an hour: the documented value, held where a change to it is seen.
func TestTheDefaultIntervalIsAnHour(t *testing.T) {
	if core.DefaultSummaryEvery != time.Hour {
		t.Fatalf("the default interval is %v, which the contract says is an hour", core.DefaultSummaryEvery)
	}
}

// What moves work is a message about it: an agent started, or a handoff, a job's end or an agent that
// stopped without a handoff reaching the lead. What does not: a job starting, the operator speaking, the
// lead's own turns, its reports.
func TestOnlyWorkMovingAsksTheLeadForAReport(t *testing.T) {
	const every = 10 * time.Minute
	moves := []struct {
		name string
		do   func(w *script, run core.RunID, lead, worker core.AgentID)
		asks bool
	}{
		{"a new agent", func(w *script, run core.RunID, lead, _ core.AgentID) { w.spawnWorker(run, lead) }, true},
		{"a handoff", func(w *script, _ core.RunID, _, worker core.AgentID) {
			w.command(core.AgentCaller{Agent: worker}, core.PostHandoff{Handoff: done()})
		}, true},
		{"a job's end", func(w *script, run core.RunID, _, _ core.AgentID) {
			w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobSucceeded})
		}, true},
		{"an agent that stopped without a handoff", func(w *script, _ core.RunID, lead, worker core.AgentID) {
			w.command(core.AgentCaller{Agent: lead}, core.StopAgent{Agent: worker})
			w.endTurn(worker, core.TurnInterrupted)
			w.fact(worker, core.SessionClosed{})
		}, true},
		{"a job starting", func(w *script, run core.RunID, _, _ core.AgentID) { w.step(core.JobsRunning{Run: run, Count: 1}) }, false},
		{"the operator speaking", func(w *script, _ core.RunID, lead, _ core.AgentID) {
			w.command(operator, core.SendMessage{Agent: lead, Text: "how is it going"})
			w.endTurn(lead, core.TurnCompleted)
		}, false},
		{"a progress report", func(w *script, _ core.RunID, lead, _ core.AgentID) { w.post(lead, core.ReportProgress) }, false},
	}
	for _, tc := range moves {
		t.Run(tc.name, func(t *testing.T) {
			w, run, leadID := scheduledRun(t, every, 3, steers)
			worker := w.spawn(run, leadID, steers)
			// The worker's start moved work; the lead's report takes it into account and starts the interval over.
			w.post(leadID, core.ReportProgress)
			base := w.runOf(run).LastSummaryAt
			if got := w.runOf(run); !got.NextSummaryAt.IsZero() || !base.Equal(w.now) {
				t.Fatalf("after the lead reported, the run is %+v", got)
			}
			tc.do(w, run, leadID, worker)
			got := w.runOf(run)
			if tc.asks {
				if want := base.Add(every); !got.NextSummaryAt.Equal(want) {
					t.Fatalf("work moved, and the lead is to be asked at %v, want %v", got.NextSummaryAt, want)
				}
			} else if !got.NextSummaryAt.IsZero() {
				t.Fatalf("something that is not work moving made the lead due to be asked at %v", got.NextSummaryAt)
			}
		})
	}
}

// The first movement arms the timer, the timer asks once and the next movement starts it again.
func TestTheLeadIsAskedOnceWhenTheTimerFiresAndNotAgainUntilWorkMovesAgain(t *testing.T) {
	const every = 20 * time.Minute
	w, run, leadID := scheduledRun(t, every, 3, steers)
	start := w.runOf(run).CreatedAt
	r := w.spawnWorker(run, leadID)
	timer, ok := w.summaryTimer()
	if !ok || timer.Run != run || !timer.FireAt.Equal(start.Add(every)) {
		t.Fatalf("work moved at the start and armed %+v, want a timer at %v", timer, start.Add(every))
	}
	if got := w.runOf(run).NextSummaryAt; !got.Equal(timer.FireAt) {
		t.Fatalf("the run is due to be asked at %v, its timer is at %v", got, timer.FireAt)
	}
	w.open(r.Agent, steers)
	w.command(core.AgentCaller{Agent: r.Agent}, core.PostHandoff{Handoff: done()})
	if _, again := w.summaryTimer(); again {
		t.Fatal("a second movement armed a second timer")
	}
	w.endTurn(leadID, core.TurnCompleted)

	w.fire(timer)
	if got := w.requests(); !slices.Equal(got, []core.ReportDue{{Every: every, Reports: 0}}) {
		t.Fatalf("the lead was asked %+v", got)
	}
	if got := w.turn(leadID).Trigger; got != core.TriggerSchedule {
		t.Fatalf("the lead woke for %s, want %s", got, core.TriggerSchedule)
	}
	if got := w.runOf(run); !got.NextSummaryAt.IsZero() || !got.LastSummaryAt.Equal(w.now) {
		t.Fatalf("after the request the run is %+v", got)
	}

	w.step(core.TimerFired{Timer: timer})
	if len(w.events) != 0 || len(w.requests()) != 1 {
		t.Fatalf("the same timer fired again and wrote %v", w.events)
	}

	w.endTurn(leadID, core.TurnCompleted)
	w.step(core.JobUpdate{Job: "j9", Run: run, State: core.JobFailed})
	again, ok := w.summaryTimer()
	if want := w.runOf(run).LastSummaryAt.Add(every); !ok || !again.FireAt.Equal(want) {
		t.Fatalf("work moved after the request and armed %+v, want a timer at %v", again, want)
	}
}

// The request is due at the interval after the last report or request, and at once when that has
// passed: a lead is not asked early, and not made to wait for a boundary it has missed.
func TestTheRequestIsDueAtTheIntervalOrAtOnceWhenThatHasPassed(t *testing.T) {
	r := rand.New(rand.NewPCG(21, 22))
	for range 300 {
		every := time.Duration(5+r.IntN(300)) * time.Minute
		quiet := time.Duration(r.Int64N(int64(3*every/time.Second))) * time.Second
		w, run, leadID := scheduledRun(t, every, 2, steers)
		start := w.runOf(run).CreatedAt
		w.now = w.now.Add(quiet)
		w.spawnWorker(run, leadID)
		want := start.Add(every)
		if w.now.After(want) {
			want = w.now
		}
		timer, ok := w.summaryTimer()
		if got := w.runOf(run).NextSummaryAt; !ok || !got.Equal(want) || !timer.FireAt.Equal(want) {
			t.Fatalf("every %v, quiet %v: due at %v with a timer at %v (armed: %t), want %v", every, quiet, got, timer.FireAt, ok, want)
		}
	}
}

// A report the lead posts unasked counts the interval from its own time, and the timer armed for the
// old time does nothing when it fires, whether the report came before or after it was due.
func TestAReportTheLeadPostsItselfRestartsTheInterval(t *testing.T) {
	const every = 30 * time.Minute
	w, run, leadID := scheduledRun(t, every, 3, steers)
	w.spawnWorker(run, leadID)
	old, ok := w.summaryTimer()
	if !ok {
		t.Fatal("work moved and armed no timer")
	}
	w.now = w.now.Add(20 * time.Minute)
	w.post(leadID, core.ReportProgress)
	reported := w.now
	if got := w.runOf(run); !got.NextSummaryAt.IsZero() || !got.LastSummaryAt.Equal(reported) {
		t.Fatalf("after the lead's report the run is %+v", got)
	}

	w.fire(old)
	if len(w.events) != 0 || len(w.requests()) != 0 {
		t.Fatalf("the timer of the schedule the report ended wrote %v", w.events)
	}

	w.now = w.now.Add(time.Minute)
	w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobSucceeded})
	next, ok := w.summaryTimer()
	if want := reported.Add(every); !ok || !next.FireAt.Equal(want) {
		t.Fatalf("work moved after the report and armed %+v, want a timer at %v", next, want)
	}

	// The old timer comes due before the new one, and the schedule it belonged to is not the run's.
	w.fire(old)
	if got := w.runOf(run); len(w.events) != 0 || !got.NextSummaryAt.Equal(next.FireAt) || len(w.requests()) != 0 {
		t.Fatalf("the old timer fired in front of the new one and wrote %v, leaving %+v", w.events, got)
	}
	w.fire(next)
	if len(w.requests()) != 1 {
		t.Fatalf("the timer the run holds asked %+v", w.requests())
	}
}

// A report of either kind cancels the request that waits for its timer; after a final report the lead
// has finished, so work that arrives late does not ask it again, and work it starts does.
func TestNoRequestIsMadeToALeadThatHasGivenItsFinalReport(t *testing.T) {
	w, run, leadID := scheduledRun(t, 10*time.Minute, 3, steers)
	w.spawnWorker(run, leadID)
	timer, ok := w.summaryTimer()
	if !ok {
		t.Fatal("work moved and armed no timer")
	}
	w.post(leadID, core.ReportFinal)
	if got := w.runOf(run); !got.FinalReported || !got.NextSummaryAt.IsZero() {
		t.Fatalf("after its final report the run is %+v", got)
	}
	w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobSucceeded})
	if got := w.runOf(run); !got.NextSummaryAt.IsZero() {
		t.Fatalf("a job that ended after the final report made the lead due to be asked at %v", got.NextSummaryAt)
	}
	w.fire(timer)
	if len(w.requests()) != 0 {
		t.Fatalf("the lead that had finished was asked: %+v", w.requests())
	}
	w.spawnWorker(run, leadID)
	if got := w.runOf(run); got.FinalReported || got.NextSummaryAt.IsZero() {
		t.Fatalf("a lead that started new work is not finished and is due to be asked, but the run is %+v", got)
	}
}

// A run that is stopping is not asked, and neither is one whose lead was told to finish: the one report
// that matters is the final one.
func TestNoRequestIsMadeOnceTheRunStopsOrReachesALimit(t *testing.T) {
	t.Run("stopping", func(t *testing.T) {
		w, run, leadID := scheduledRun(t, 10*time.Minute, 3, steers)
		w.spawnWorker(run, leadID)
		timer, _ := w.summaryTimer()
		w.command(operator, core.StopRun{Run: run})
		if got := w.runOf(run); !got.NextSummaryAt.IsZero() {
			t.Fatalf("a run that is stopping is still due to ask at %v", got.NextSummaryAt)
		}
		w.fire(timer)
		if len(w.requests()) != 0 || len(w.events) != 0 {
			t.Fatalf("a stopping run wrote %v", w.events)
		}
	})
	t.Run("a limit", func(t *testing.T) {
		w, run, leadID, deadline := limitedRun(t, time.Hour, 0)
		w.spawnWorker(run, leadID)
		timer, _ := w.summaryTimer()
		if timer.FireAt.Before(w.runOf(run).CreatedAt.Add(core.DefaultSummaryEvery)) {
			t.Fatalf("the timer is at %v, before the default interval", timer.FireAt)
		}
		w.fire(deadline)
		got := w.runOf(run)
		if got.LimitReached != core.LimitTime || !got.NextSummaryAt.IsZero() {
			t.Fatalf("at the time limit the run is %+v", got)
		}
		w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobSucceeded})
		if got := w.runOf(run); !got.NextSummaryAt.IsZero() {
			t.Fatalf("a job's end after the limit made the lead due to be asked at %v", got.NextSummaryAt)
		}
		w.fire(timer)
		if len(w.requests()) != 0 {
			t.Fatalf("the lead that was told to finish was asked for a progress report: %+v", w.requests())
		}
	})
}

// A request is a message like the others the lead gets: it is never turned away because a run is at
// its fan-out limit or its lead is busy, steers into a turn that is running and waits for the next
// turn of a lead that cannot take a steer.
func TestARequestIsNeverTurnedAwayForTheFanoutLimitOrABusyLead(t *testing.T) {
	for _, tc := range []struct {
		name string
		caps core.Capabilities
		want core.Delivery
	}{{"a lead that steers", steers, core.DeliverySteered}, {"a lead that does not", noSteer, core.DeliveryQueued}} {
		t.Run(tc.name, func(t *testing.T) {
			w, run, leadID := scheduledRun(t, 10*time.Minute, 1, tc.caps)
			// The limit is one live subagent, and it is live.
			r := w.spawnWorker(run, leadID)
			timer, _ := w.summaryTimer()
			w.open(r.Agent, steers)
			if refused := w.command(core.AgentCaller{Agent: leadID}, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "more"}); refused.Refusal != core.RefuseFanoutLimit {
				t.Fatalf("a second subagent at a limit of one: %q", refused.Refusal)
			}
			// The lead is working: the worker's handoff started its turn.
			w.command(core.AgentCaller{Agent: r.Agent}, core.PostHandoff{Handoff: done()})
			if got := w.status(leadID); got != core.AgentActive {
				t.Fatalf("the lead is %s, want active", got)
			}
			w.fire(timer)
			var request core.Message
			for _, e := range w.events {
				if m, ok := e.Body.(core.Message); ok {
					if _, ok := m.Body.(core.ReportDue); ok {
						request = m
					}
				}
			}
			if request.ID == "" || request.To != leadID || request.Delivery != tc.want || request.Rejection != "" {
				t.Fatalf("the request is %+v, want one delivered as %s", request, tc.want)
			}
			w.endTurn(leadID, core.TurnCompleted)
			if tc.want == core.DeliveryQueued {
				if got := w.turn(leadID); got.Trigger != core.TriggerSchedule || got.Message != request.ID {
					t.Fatalf("the lead's next turn is %+v, want one for the request", got)
				}
			}
		})
	}
}

// A lead that has not read a request is not sent another: it will report on the whole run when it
// does, so the movement since counts as asked and no second message waits behind the first.
func TestARequestTheLeadHasNotReadIsNotRepeated(t *testing.T) {
	const every = 10 * time.Minute
	w, run, leadID := scheduledRun(t, every, 3, noSteer)
	w.spawnWorker(run, leadID)
	first, _ := w.summaryTimer()
	w.command(operator, core.SendMessage{Agent: leadID, Text: "keep going"}) // the lead is busy, and cannot take a steer
	w.fire(first)
	if got := w.requests(); len(got) != 1 {
		t.Fatalf("after the first timer the lead was asked %+v", got)
	}
	// Two intervals pass with the lead still in that turn, and work moves.
	w.now = w.now.Add(2 * every)
	w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobSucceeded})
	second, ok := w.summaryTimer()
	if !ok {
		t.Fatal("work moved after the request and armed no timer")
	}
	w.fire(second)
	if got := w.requests(); len(got) != 1 {
		t.Fatalf("a request the lead had not read was followed by another: %+v", got)
	}
	if got := w.runOf(run); !got.NextSummaryAt.IsZero() || !got.LastSummaryAt.Equal(w.now) {
		t.Fatalf("the movement was not counted as asked: %+v", got)
	}
}

// A run recorded before the schedule existed has no interval, and is never asked: replay does not
// invent one.
func TestARunRecordedBeforeTheScheduleExistedIsNeverAsked(t *testing.T) {
	w := newScript(t)
	run := core.Run{
		ID: "run-1", Repository: "repo", Task: "task", Status: core.RunRunning, Lead: "lead-1", FanoutLimit: 2, CreatedAt: epoch,
		Allowed: []core.AgentChoice{{Harness: lead.Harness, Model: lead.Model}},
	}
	agent := core.Agent{
		ID: "lead-1", Run: run.ID, Role: core.RoleLead, Spec: lead, Status: core.AgentIdle, Session: 1, Capabilities: steers, StartedAt: epoch,
		ResumeToken: "conv-lead-1",
	}
	w.s = core.Replay(core.State{}, []core.Event{
		{Run: run.ID, At: epoch, Body: run}, {Run: run.ID, At: epoch, Body: agent},
	})
	w.now = epoch.Add(time.Minute)
	w.spawnWorker(run.ID, agent.ID)
	w.step(core.JobUpdate{Job: "j1", Run: run.ID, State: core.JobSucceeded})
	if got := w.runOf(run.ID); !got.NextSummaryAt.IsZero() || got.SummaryEvery != 0 {
		t.Fatalf("work moved in a run with no schedule and it is %+v", got)
	}
	if _, armed := w.summaryTimer(); armed {
		t.Fatal("work moved in a run with no schedule and armed a timer")
	}
}

// The simulation with the weight on what the schedule depends on: long pauses, reports, timers that fire
// again, crashes that restart the daemon between a timer's arming and its firing, and several runs at
// once. Each seed holds the same monitor as TestSimulation.
func TestTheReportScheduleHoldsInRunsThatPauseAndReport(t *testing.T) {
	var total coverage
	for seed := range uint64(24) {
		w := newSim(t, 10_000+seed)
		w.plan = []action{
			{40, w.finishStart}, {30, w.startAnotherRun}, {100, w.spawn}, {60, w.send}, {70, w.handoff}, {60, w.pollJobs}, {60, w.report},
			{70, w.pause}, {30, w.lull}, {10, w.stopSubagent}, {2, w.stopRun}, {20, w.retry}, {20, w.crash},
			{10, w.killSidecar}, {60, func() { w.fireTimer(false) }}, {200, w.work}, {160, w.act}, {230, func() { w.readSidecar() }},
		}
		w.startRun()
		for range 900 {
			w.step()
		}
		w.settle()
		total.add(w.monitor.covered)
	}
	t.Logf("coverage: %+v", total.schedule)
	for name, n := range map[string]int{
		"requests for a report": total.schedule.asks, "requests that were still unread": total.schedule.swallowed,
		"stale summary timers": total.schedule.stale, "reports that restarted the interval": total.schedule.restarted,
		"requests taken away": total.schedule.cancelled,
	} {
		if n == 0 {
			t.Errorf("the simulation never reached: %s", name)
		}
	}
}
