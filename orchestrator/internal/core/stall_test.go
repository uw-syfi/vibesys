package core_test

import (
	"math/rand/v2"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// runOf is the run as the state holds it.
func (w *script) runOf(id core.RunID) core.Run {
	w.t.Helper()
	run, ok := w.s.Run(id)
	if !ok {
		w.t.Fatalf("no run %s", id)
	}
	return run
}

// leadTimer is the lead-idle timer the last step armed, if it armed one.
func (w *script) leadTimer() (core.ArmTimer, bool) {
	for _, e := range w.effects {
		if t, ok := e.(core.ArmTimer); ok && t.Kind == core.TimerLeadIdle {
			return t, true
		}
	}
	return core.ArmTimer{}, false
}

// fire answers a timer at its time.
func (w *script) fire(t core.ArmTimer) {
	w.t.Helper()
	w.now = t.FireAt.Add(-time.Second)
	w.step(core.TimerFired{Timer: t})
}

// bodies are the message bodies the last step wrote, in order.
func (w *script) bodies() []core.MessageBody {
	var out []core.MessageBody
	for _, e := range w.events {
		if m, ok := e.Body.(core.Message); ok {
			out = append(out, m.Body)
		}
	}
	return out
}

// sent counts the distinct messages with this body that the script has ever written.
func (w *script) sent(body core.MessageBody) int {
	ids := map[core.MessageID]bool{}
	for _, e := range w.log {
		if m, ok := e.Body.(core.Message); ok && m.Body == body {
			ids[m.ID] = true
		}
	}
	return len(ids)
}

func (w *script) wrote(body core.MessageBody) bool {
	for _, b := range w.bodies() {
		if b == body {
			return true
		}
	}
	return false
}

// stall runs a lead to rest with nothing outstanding and returns the timer that was armed for it.
func stalledRun(t *testing.T) (*script, core.RunID, core.AgentID, core.ArmTimer) {
	t.Helper()
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: 2, Lead: lead, Brief: "go"})
	w.open(r.Agent, steers)
	w.endTurn(r.Agent, core.TurnCompleted)
	timer, ok := w.leadTimer()
	if !ok {
		t.Fatalf("a lead that rested with nothing outstanding armed no timer: %v", w.kinds())
	}
	return w, r.Run, r.Agent, timer
}

// A lead that rests with nothing to wait for has stopped the run, and the run says since when.
func TestARunWhoseLeadRestsWithNothingOutstandingIsStalledFromThatMoment(t *testing.T) {
	w, run, _, timer := stalledRun(t)
	got := w.runOf(run)
	if got.StalledSince.IsZero() || !got.StalledSince.Equal(w.now) {
		t.Fatalf("stalled since %v, want %v", got.StalledSince, w.now)
	}
	if want := w.now.Add(core.LeadIdleAfter); !timer.FireAt.Equal(want) {
		t.Fatalf("the reminder is due at %v, want %v", timer.FireAt, want)
	}
}

// The stall ends as soon as the lead is given anything to do.
func TestAnyInputToTheLeadEndsTheStall(t *testing.T) {
	w, run, leadID, _ := stalledRun(t)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
	if got := w.runOf(run); !got.StalledSince.IsZero() {
		t.Fatalf("stalled since %v while the lead runs a turn", got.StalledSince)
	}
	w.endTurn(leadID, core.TurnCompleted)
	if got := w.runOf(run); got.StalledSince.IsZero() {
		t.Fatal("the run is not stalled after the lead rested again")
	}
}

// A running job or a working subagent is something the lead is waiting for.
func TestARunWithAJobOrAWorkingAgentIsNotStalled(t *testing.T) {
	w, run, leadID, _ := stalledRun(t)
	w.step(core.JobsRunning{Run: run, Count: 1})
	if got := w.runOf(run); !got.StalledSince.IsZero() || got.ActiveJobs != 1 {
		t.Fatalf("with a job going: %+v", got)
	}
	w.step(core.JobsRunning{Run: run, Count: 1})
	if len(w.events) != 0 {
		t.Fatalf("the same job count recorded %v", w.events)
	}
	worker := w.spawn(run, leadID, steers)
	w.step(core.JobsRunning{Run: run, Count: 0})
	if got := w.runOf(run); !got.StalledSince.IsZero() {
		t.Fatalf("stalled with %s working", worker)
	}
	w.command(core.AgentCaller{Agent: worker}, core.PostHandoff{Handoff: core.Handoff{Claim: "done"}})
	w.endTurn(leadID, core.TurnCompleted)
	w.endTurn(worker, core.TurnCompleted)
	w.fact(worker, core.SessionClosed{})
	if got := w.runOf(run); got.StalledSince.IsZero() {
		t.Fatalf("not stalled after the worker finished: %+v", got)
	}
}

// The reminder is one typed message, once for the stall, and starts a turn the lead can act in.
func TestAStalledLeadIsRemindedOnceAndWakes(t *testing.T) {
	w, run, leadID, timer := stalledRun(t)
	w.fire(timer)
	if !w.wrote(core.LeadIdleReminder{Idle: core.LeadIdleAfter}) {
		t.Fatalf("no reminder among %v", w.bodies())
	}
	if got := w.turn(leadID).Trigger; got != core.TriggerReminder {
		t.Fatalf("the lead woke for %s", got)
	}
	if got := w.runOf(run); got.LeadReminders != 1 || got.Status != core.RunRunning || !got.StalledSince.IsZero() {
		t.Fatalf("after the reminder: %+v", got)
	}
	w.step(core.TimerFired{Timer: timer})
	if len(w.events) != 0 {
		t.Fatalf("the same timer fired twice and recorded %v", w.events)
	}
}

// A lead that ignores every reminder is left alone after the third, and the stall stays visible.
func TestALeadIsRemindedAtMostThreeTimesInARun(t *testing.T) {
	w, run, leadID, timer := stalledRun(t)
	for i := 1; i <= core.MaxLeadReminders; i++ {
		w.fire(timer)
		w.endTurn(leadID, core.TurnCompleted)
		var ok bool
		if timer, ok = w.leadTimer(); ok != (i < core.MaxLeadReminders) {
			t.Fatalf("after reminder %d a timer was armed: %t", i, ok)
		}
	}
	got := w.runOf(run)
	if got.LeadReminders != core.MaxLeadReminders || got.StalledSince.IsZero() || got.Status != core.RunRunning {
		t.Fatalf("after the last reminder: %+v", got)
	}
	w.fire(core.ArmTimer{Kind: core.TimerLeadIdle, Run: run, FireAt: got.StalledSince.Add(core.LeadIdleAfter)})
	if len(w.events) != 0 {
		t.Fatalf("a fourth reminder recorded %v", w.events)
	}
}

// A timer from a stall that ended says nothing about the one that began after it.
func TestAReminderTimerOfAnEarlierStallDoesNothing(t *testing.T) {
	w, run, leadID, old := stalledRun(t)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
	w.endTurn(leadID, core.TurnCompleted)
	if _, ok := w.leadTimer(); !ok {
		t.Fatal("the second stall armed no timer")
	}
	w.now = old.FireAt
	w.step(core.TimerFired{Timer: old})
	if len(w.events) != 0 || w.runOf(run).LeadReminders != 0 {
		t.Fatalf("the old timer recorded %v", w.events)
	}
}

// Nothing is said to the lead of a run that is over or on its way to being over.
func TestNoReminderIsSentOnceTheRunIsStopping(t *testing.T) {
	w, run, _, timer := stalledRun(t)
	w.command(operator, core.StopRun{Run: run})
	w.fire(timer)
	if len(w.events) != 0 {
		t.Fatalf("a stopping run recorded %v", w.events)
	}
}

// A subagent that goes quiet without a handoff is asked once to hand off; the lead hears of it only
// if it goes quiet again.
func TestASubagentWithoutAHandoffIsRemindedOncePerSession(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	w.endTurn(a, core.TurnCompleted)
	if !w.wrote(core.HandoffReminder{}) || w.wrote(core.SubagentSettled{Agent: a, Status: core.AgentIdle}) {
		t.Fatalf("after the first quiet turn: %v", w.bodies())
	}
	if got := w.turn(a).Trigger; got != core.TriggerReminder {
		t.Fatalf("the subagent woke for %s", got)
	}
	if _, running := w.s.ActiveTurn(leadID); running {
		t.Fatal("the lead woke before the subagent had been reminded")
	}
	w.endTurn(a, core.TurnCompleted)
	if !w.wrote(core.SubagentSettled{Agent: a, Status: core.AgentIdle}) || w.sent(core.HandoffReminder{}) != 1 {
		t.Fatalf("after the second quiet turn: %v", w.bodies())
	}
	if got := w.turn(leadID).Trigger; got != core.TriggerSubagentSettled {
		t.Fatalf("the lead woke for %s", got)
	}
}

// A new session is a new chance to hand off.
func TestAResumedSubagentIsRemindedAgain(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	w.endTurn(a, core.TurnCompleted)
	w.endTurn(a, core.TurnCompleted)
	w.endTurn(leadID, core.TurnCompleted)
	w.fact(a, core.SessionClosed{})
	w.command(operator, core.SendMessage{Agent: a, Text: "again"})
	w.open(a, steers)
	w.endTurn(a, core.TurnCompleted)
	if w.sent(core.HandoffReminder{}) != 2 {
		t.Fatalf("the resumed session was not reminded: %d reminders in all", w.sent(core.HandoffReminder{}))
	}
}

// A subagent that handed off is not reminded of anything.
func TestASubagentThatHandedOffIsNeverReminded(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	w.command(core.AgentCaller{Agent: a}, core.PostHandoff{Handoff: core.Handoff{Claim: "done"}})
	w.endTurn(a, core.TurnCompleted)
	if w.sent(core.HandoffReminder{}) != 0 {
		t.Fatal("an agent that handed off was reminded")
	}
}

// Reports are numbered within their run, in the order they are recorded, and carry their kind.
func TestALeadThatGaveItsFinalReportIsFinishedNotStalled(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "finish"})
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
	w.endTurn(leadID, core.TurnCompleted)
	got := w.runOf(run)
	if !got.FinalReported || !got.StalledSince.IsZero() {
		t.Fatalf("after a final report and a rest: %+v", got)
	}
	if _, armed := w.leadTimer(); armed {
		t.Fatal("a timer was armed to remind a lead that has finished")
	}

	// New input puts the lead back to work, and the usual rule applies when it rests again.
	w.command(operator, core.SendMessage{Agent: leadID, Text: "one more thing"})
	if got := w.runOf(run); got.FinalReported {
		t.Fatalf("still finished while it works on new input: %+v", got)
	}
	w.endTurn(leadID, core.TurnCompleted)
	if got := w.runOf(run); got.StalledSince.IsZero() {
		t.Fatal("a lead that rested without a report is not stalled")
	}
}

// A progress report after a final one says the lead is going on.
func TestAProgressReportAfterAFinalOneReturnsTheRunToTheNormalRule(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "finish"})
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "more"}})
	w.endTurn(leadID, core.TurnCompleted)
	if got := w.runOf(run); got.FinalReported || got.StalledSince.IsZero() {
		t.Fatalf("a progress report after a final one: %+v", got)
	}
}

func TestReportsAreNumberedWithinTheirRun(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	for i, kind := range []core.ReportKind{core.ReportProgress, core.ReportProgress, core.ReportFinal} {
		w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: kind, Headline: "h"}})
		var got core.Report
		for _, b := range w.bodies() {
			if r, ok := b.(core.Report); ok {
				got = r
			}
		}
		if got.Sequence != uint32(i+1) || got.Kind != kind || w.runOf(run).ReportsPosted != uint32(i+1) {
			t.Fatalf("report %d is %+v, the run counts %d", i+1, got, w.runOf(run).ReportsPosted)
		}
	}
}

// The usage of finished turns adds up per agent and per run; a provider that reports no dollars
// leaves the cost absent and never zero.
func TestUsageAddsUpPerAgentAndPerRunWithoutInventingACost(t *testing.T) {
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: 1, Lead: lead, Brief: "go"})
	w.open(r.Agent, steers)
	tokens := func(n uint64) *uint64 { return &n }
	w.fact(r.Agent, core.TurnEnded{Turn: w.turn(r.Agent).ID, Status: core.TurnCompleted, Usage: core.Usage{InputTokens: tokens(10), OutputTokens: tokens(1)}})
	w.command(operator, core.SendMessage{Agent: r.Agent, Text: "more"})
	w.fact(r.Agent, core.TurnEnded{Turn: w.turn(r.Agent).ID, Status: core.TurnCompleted, Usage: core.Usage{InputTokens: tokens(5)}})
	for name, u := range map[string]core.Usage{"agent": agentOf(w, r.Agent).Usage, "run": w.runOf(r.Run).Usage} {
		if u.InputTokens == nil || *u.InputTokens != 15 || u.OutputTokens == nil || *u.OutputTokens != 1 || u.CostUSD != nil {
			t.Fatalf("%s usage = %+v, want 15 in, 1 out, no cost", name, u)
		}
	}
	cost := 0.25
	w.command(operator, core.SendMessage{Agent: r.Agent, Text: "again"})
	w.fact(r.Agent, core.TurnEnded{Turn: w.turn(r.Agent).ID, Status: core.TurnCompleted, Usage: core.Usage{CostUSD: &cost}})
	if got := w.runOf(r.Run).Usage.CostUSD; got == nil || *got != cost {
		t.Fatalf("run cost = %v, want %v", got, cost)
	}
}

func agentOf(w *script, id core.AgentID) core.Agent {
	w.t.Helper()
	a, ok := w.s.Agent(id)
	if !ok {
		w.t.Fatalf("no agent %s", id)
	}
	return a
}

// Over any sequence of the things that move a run, the rules hold at every step: a run's lead is
// reminded at most three times, never while a job or an agent is going, never after the run stops
// being live, and the stall is set exactly when the run stands still (State.Violations, which the
// script checks after each step, holds the last).
func TestLeadRemindersObeyTheirRulesOverRandomSequences(t *testing.T) {
	for seed := range uint64(200) {
		r := rand.New(rand.NewPCG(seed, seed+1))
		w, run, leadID := started(t, 2, steers)
		var timers []core.ArmTimer
		var worker core.AgentID
		for range 120 {
			before := w.runOf(run)
			switch r.IntN(8) {
			case 0:
				w.step(core.JobsRunning{Run: run, Count: uint32(r.IntN(2))})
			case 1:
				if a, ok := w.s.Agent(leadID); ok && a.Status.Resting() {
					w.command(operator, core.SendMessage{Agent: leadID, Text: "go"})
				}
			case 2:
				if _, running := w.s.ActiveTurn(leadID); running {
					w.endTurn(leadID, core.TurnCompleted)
				}
			case 3:
				if worker == "" && before.Status == core.RunRunning {
					if a := w.command(core.AgentCaller{Agent: leadID}, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "b"}); a.Refusal == "" {
						worker = a.Agent
						w.open(worker, steers)
					}
				}
			case 4:
				if _, running := w.s.ActiveTurn(worker); running && worker != "" {
					w.endTurn(worker, core.TurnCompleted)
				}
			case 5:
				if len(timers) > 0 {
					i := r.IntN(len(timers))
					w.now = latest(w.now, timers[i].FireAt)
					w.step(core.TimerFired{Timer: timers[i]})
				}
			case 6:
				if _, running := w.s.ActiveTurn(leadID); running {
					kind := core.ReportProgress
					if r.IntN(2) == 0 {
						kind = core.ReportFinal
					}
					w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: kind, Headline: "h"}})
				}
			default:
				if r.IntN(25) == 0 {
					w.command(operator, core.StopRun{Run: run})
				}
			}
			for _, e := range w.effects {
				if timer, ok := e.(core.ArmTimer); ok && timer.Kind == core.TimerLeadIdle {
					timers = append(timers, timer)
				}
			}
			after := w.runOf(run)
			if after.LeadReminders > core.MaxLeadReminders {
				t.Fatalf("seed %d: %d reminders", seed, after.LeadReminders)
			}
			if after.FinalReported && (!after.StalledSince.IsZero() || after.LeadReminders > before.LeadReminders) {
				t.Fatalf("seed %d: a lead that gave its final report is stalled or reminded: %+v", seed, after)
			}
			if after.LeadReminders > before.LeadReminders {
				if before.Status != core.RunRunning || before.ActiveJobs != 0 || before.StalledSince.IsZero() {
					t.Fatalf("seed %d: a reminder from %+v", seed, before)
				}
			}
		}
	}
}
