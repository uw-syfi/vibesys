package core_test

import (
	"math/rand/v2"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// limitedRun starts a run with a time limit of maxTime (none when zero) and a cost limit of maxCost
// dollars (none when zero), opens its lead and lets the lead rest after its brief. It returns the
// timer that will reach the time limit.
func limitedRun(t *testing.T, maxTime time.Duration, maxCost float64) (*script, core.RunID, core.AgentID, core.ArmTimer) {
	t.Helper()
	w := newScript(t)
	r := w.command(operator, core.StartRun{
		Repository: "repo", Task: "task", FanoutLimit: 3, MaxTime: maxTime, MaxCostUSD: maxCost, Lead: lead, Brief: "go",
	})
	var timer core.ArmTimer
	for _, e := range w.effects {
		if a, ok := e.(core.ArmTimer); ok && a.Kind == core.TimerRunDeadline {
			timer = a
		}
	}
	if timer.Kind == "" != (maxTime == 0) {
		t.Fatalf("a time limit of %v armed %+v", maxTime, timer)
	}
	w.open(r.Agent, steers)
	w.endTurn(r.Agent, core.TurnCompleted)
	return w, r.Run, r.Agent, timer
}

// endTurnCosting ends the agent's running turn, which cost usd dollars.
func (w *script) endTurnCosting(a core.AgentID, usd float64) {
	w.t.Helper()
	w.fact(a, core.TurnEnded{Turn: w.turn(a).ID, Status: core.TurnCompleted, Usage: core.Usage{CostUSD: &usd}})
}

// limitMessages are the messages that told a lead its run reached a limit, in the order written.
func (w *script) limitMessages() []core.RunLimitReached {
	var out []core.RunLimitReached
	seen := map[core.MessageID]bool{}
	for _, e := range w.log {
		if m, ok := e.Body.(core.Message); ok && !seen[m.ID] {
			if b, ok := m.Body.(core.RunLimitReached); ok {
				seen[m.ID] = true
				out = append(out, b)
			}
		}
	}
	return out
}

// graceTimer is the timer the last step armed to end the wait for the lead, if it armed one.
func (w *script) graceTimer() (core.ArmTimer, bool) {
	for _, e := range w.effects {
		if t, ok := e.(core.ArmTimer); ok && t.Kind == core.TimerLimitGrace {
			return t, true
		}
	}
	return core.ArmTimer{}, false
}

func (w *script) spawnWorker(run core.RunID, by core.AgentID) core.Receipt {
	w.t.Helper()
	return w.command(core.AgentCaller{Agent: by}, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "work"})
}

// The deadline no longer stops the run: it tells the lead, once, to finish, and the run goes on until
// the lead has or the grace has passed.
func TestTheTimeLimitTellsTheLeadToFinishAndStopsTheRunAfterTheGrace(t *testing.T) {
	w, run, leadID, timer := limitedRun(t, time.Hour, 0)
	w.spawn(run, leadID, steers)
	deadline := w.runOf(run).Deadline

	w.fire(timer)
	got := w.runOf(run)
	if got.Status != core.RunRunning || got.LimitReached != core.LimitTime || !got.LimitReachedAt.Equal(deadline) {
		t.Fatalf("at the time limit the run is %+v", got)
	}
	told := w.limitMessages()
	if len(told) != 1 || told[0].Limit != core.LimitTime || told[0].Grace != core.LimitGrace || told[0].Elapsed != deadline.Sub(got.CreatedAt) {
		t.Fatalf("the lead was told %+v", told)
	}
	if turn := w.turn(leadID); turn.Trigger != core.TriggerReminder {
		t.Fatalf("the lead's turn for the limit is %+v", turn)
	}
	grace, ok := w.graceTimer()
	if !ok || !grace.FireAt.Equal(deadline.Add(core.LimitGrace)) {
		t.Fatalf("grace timer %+v armed: %v", grace, ok)
	}

	// Nothing repeats it, and no new agent starts.
	w.fire(timer)
	if len(w.events) != 0 {
		t.Fatalf("a repeated deadline recorded %v", w.events)
	}
	if r := w.spawnWorker(run, leadID); r.Refusal != core.RefuseTimeLimitReached {
		t.Fatalf("a spawn after the time limit: %+v", r)
	}
	if n := len(w.s.Agents(run)); n != 2 {
		t.Fatalf("the run has %d agents, want its lead and the one worker", n)
	}

	w.fire(grace)
	if got := w.runOf(run); got.Status != core.RunStopping || got.StopReason != core.StopTimeLimit {
		t.Fatalf("after the grace the run is %+v", got)
	}
	w.fire(grace)
	if len(w.events) != 0 {
		t.Fatalf("a repeated grace recorded %v", w.events)
	}
}

// A run that stops for its limit ends as ended, not failed, with the reason it was stopped for.
func TestARunStoppedByItsLimitEndsAndSaysWhy(t *testing.T) {
	w, run, leadID, timer := limitedRun(t, time.Hour, 0)
	w.fire(timer)
	grace, _ := w.graceTimer()
	w.fire(grace)
	w.endTurn(leadID, core.TurnInterrupted)
	w.fact(leadID, core.SessionClosed{})
	if got := w.runOf(run); got.Status != core.RunEnded || got.StopReason != core.StopTimeLimit {
		t.Fatalf("run = %+v", got)
	}
}

// The cost limit is reached by the turn whose end brings the finished turns' cost to it, counting only
// what a turn reported.
func TestTheCostLimitIsReachedByTheTurnThatBringsTheCostToIt(t *testing.T) {
	w, run, leadID, _ := limitedRun(t, 0, 1)

	w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
	w.endTurnCosting(leadID, 0.6)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
	w.fact(leadID, core.TurnEnded{Turn: w.turn(leadID).ID, Status: core.TurnCompleted}) // reported no cost
	if got := w.runOf(run); got.LimitReached != "" {
		t.Fatalf("the limit was reached at $0.60 of $1: %+v", got)
	}
	if r := w.spawnWorker(run, leadID); r.Refusal != "" {
		t.Fatalf("a spawn under the limit was refused: %+v", r)
	}

	w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
	w.endTurnCosting(leadID, 0.5)
	got := w.runOf(run)
	if got.LimitReached != core.LimitCost || got.Status != core.RunRunning {
		t.Fatalf("at $1.10 of $1 the run is %+v", got)
	}
	told := w.limitMessages()
	if len(told) != 1 || told[0].Limit != core.LimitCost || told[0].MaxCost != 1 || told[0].Spent == nil || *told[0].Spent != 1.1 {
		t.Fatalf("the lead was told %+v", told)
	}
	grace, ok := w.graceTimer()
	if !ok {
		t.Fatal("no grace timer was armed")
	}
	if r := w.spawnWorker(run, leadID); r.Refusal != core.RefuseCostLimitReached {
		t.Fatalf("a spawn after the cost limit: %+v", r)
	}
	w.fire(grace)
	if got := w.runOf(run); got.Status != core.RunStopping || got.StopReason != core.StopCostLimit {
		t.Fatalf("after the grace the run is %+v", got)
	}
}

// The lead that finishes as it was asked ends the run at once, with its report delivered.
func TestAFinalReportAfterALimitStopsTheRunAtOnce(t *testing.T) {
	w, run, leadID, timer := limitedRun(t, time.Hour, 0)
	w.fire(timer)
	r := w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
	if got := w.runOf(run); got.Status != core.RunStopping || got.StopReason != core.StopTimeLimit {
		t.Fatalf("after the final report the run is %+v", got)
	}
	if r.Delivery != core.DeliveryConsumed {
		t.Fatalf("the report was %s", r.Delivery)
	}

	// A progress report does not finish anything.
	w, run, leadID, timer = limitedRun(t, time.Hour, 0)
	w.fire(timer)
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "more"}})
	if got := w.runOf(run); got.Status != core.RunRunning {
		t.Fatalf("a progress report stopped the run: %+v", got)
	}
}

// A lead that had finished before the limit has nothing to be told: the run stops.
func TestALeadThatHadFinishedIsNotToldAboutALimit(t *testing.T) {
	w, run, leadID, timer := limitedRun(t, time.Hour, 0)
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
	w.fire(timer)
	if got := w.runOf(run); got.Status != core.RunStopping || got.StopReason != core.StopTimeLimit {
		t.Fatalf("run = %+v", got)
	}
	if told := w.limitMessages(); len(told) != 0 {
		t.Fatalf("a lead that had finished was told %+v", told)
	}
}

// A run that is already stopped is not given a limit afterwards, and one limit does not replace another.
func TestARunReachesOneLimitOnce(t *testing.T) {
	w, run, leadID, timer := limitedRun(t, time.Hour, 1)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go on"})
	w.endTurnCosting(leadID, 2)
	if got := w.runOf(run); got.LimitReached != core.LimitCost {
		t.Fatalf("run = %+v", got)
	}
	w.fire(timer)
	if got := w.runOf(run); got.LimitReached != core.LimitCost || len(w.limitMessages()) != 1 {
		t.Fatalf("the time limit replaced the cost limit: %+v", got)
	}

	w, run, _, timer = limitedRun(t, time.Hour, 1)
	w.command(operator, core.StopRun{Run: run})
	w.fire(timer)
	if got := w.runOf(run); got.LimitReached != "" || got.StopReason != core.StopOperator {
		t.Fatalf("a stopped run reached a limit: %+v", got)
	}
}

// The guarantees of a limit hold over any order of costs, steers, reports, spawn attempts and clock:
// the lead is told once and only when a limit is reached; every spawn after it is refused for the
// limit and none before it is; the run stops when the grace has passed, or when the lead has finished;
// and replaying the events gives the same state (checked by every step of the script).
func TestLimitsHoldOverAnyOrderOfEvents(t *testing.T) {
	for seed := range uint64(300) {
		r := rand.New(rand.NewPCG(seed, seed+7))
		var maxTime time.Duration
		if r.IntN(2) == 0 {
			maxTime = time.Duration(1+r.IntN(60)) * time.Minute
		}
		maxCost := 0.0
		if r.IntN(2) == 0 {
			maxCost = float64(1 + r.IntN(5))
		}
		w, run, leadID, deadlineTimer := limitedRun(t, maxTime, maxCost)
		timers := []core.ArmTimer{}
		if maxTime > 0 {
			timers = append(timers, deadlineTimer)
		}
		var spent float64
		spawnsAfter := 0
		for range 80 {
			// Time passes, and every timer that came due fires, earliest first.
			w.now = w.now.Add(time.Duration(r.IntN(240)) * time.Second)
			for {
				i := slices.IndexFunc(timers, func(a core.ArmTimer) bool { return !a.FireAt.After(w.now) })
				if i < 0 {
					break
				}
				timer := timers[i]
				timers = slices.Delete(timers, i, i+1)
				w.now = latest(w.now, timer.FireAt).Add(-time.Second)
				w.step(core.TimerFired{Timer: timer})
				if timer.Kind == core.TimerLimitGrace {
					if got := w.runOf(run); got.LimitReached != "" && got.Status.Live() {
						t.Fatalf("seed %d: the grace passed and the run is %+v", seed, got)
					}
				}
				timers = append(timers, armedTimers(w)...)
			}
			before := w.runOf(run)
			switch r.IntN(5) {
			case 0:
				if _, running := w.s.ActiveTurn(leadID); running && before.Status.Live() {
					usd := float64(r.IntN(4)) / 2
					spent += usd
					w.endTurnCosting(leadID, usd)
				}
			case 1:
				if a, ok := w.s.Agent(leadID); ok && a.Status.Resting() && before.Status.Live() {
					w.command(operator, core.SendMessage{Agent: leadID, Text: "go"})
				}
			case 2:
				if before.Status.Live() {
					receipt := w.spawnWorker(run, leadID)
					if receipt.Refusal == "" {
						w.open(receipt.Agent, steers)
					}
					limited := before.LimitReached != ""
					if limited != (receipt.Refusal == core.RefuseTimeLimitReached || receipt.Refusal == core.RefuseCostLimitReached) && receipt.Refusal != core.RefuseFanoutLimit {
						t.Fatalf("seed %d: with limit %q reached a spawn was %+v", seed, before.LimitReached, receipt)
					}
					if limited {
						spawnsAfter++
					}
				}
			case 3:
				if _, running := w.s.ActiveTurn(leadID); running && before.Status.Live() && r.IntN(4) == 0 {
					kind := core.ReportProgress
					if r.IntN(2) == 0 {
						kind = core.ReportFinal
					}
					w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: kind, Headline: "h"}})
				}
			default:
				if _, running := w.s.ActiveTurn(leadID); running && before.Status.Live() {
					w.endTurnCosting(leadID, 0)
				}
			}
			timers = append(timers, armedTimers(w)...)
			after := w.runOf(run)
			if told := len(w.limitMessages()); told > 1 || (told == 1) != (after.LimitReached != "" && !finishedWhenReached(w, run)) {
				t.Fatalf("seed %d: told %d times with limit %q: %+v", seed, told, after.LimitReached, after)
			}
			if after.LimitReached == core.LimitCost && !(after.Usage.CostUSD != nil && *after.Usage.CostUSD >= maxCost && spent >= maxCost) {
				t.Fatalf("seed %d: cost limit reached at $%v of $%v", seed, spent, maxCost)
			}
			if before.LimitReached != "" && after.LimitReached != before.LimitReached {
				t.Fatalf("seed %d: the limit went from %q to %q", seed, before.LimitReached, after.LimitReached)
			}
			if before.Status.Live() && maxCost > 0 && spent >= maxCost && after.LimitReached == "" && after.Status.Live() {
				t.Fatalf("seed %d: $%v spent of $%v and no limit reached: %+v", seed, spent, maxCost, after)
			}
		}
		// Whatever is left of the clock runs out: a run that reached a limit stops.
		for len(timers) > 0 {
			slices.SortFunc(timers, func(a, b core.ArmTimer) int { return a.FireAt.Compare(b.FireAt) })
			timer := timers[0]
			timers = timers[1:]
			w.now = latest(w.now, timer.FireAt).Add(-time.Second)
			w.step(core.TimerFired{Timer: timer})
			timers = append(timers, armedTimers(w)...)
		}
		if got := w.runOf(run); got.LimitReached != "" && got.Status.Live() {
			t.Fatalf("seed %d: all timers ran and the run is %+v", seed, got)
		}
		_ = spawnsAfter
	}
}

// armedTimers are the run-level timers the last step asked for: the ones a limit needs.
func armedTimers(w *script) []core.ArmTimer {
	var out []core.ArmTimer
	for _, e := range w.effects {
		if a, ok := e.(core.ArmTimer); ok && (a.Kind == core.TimerLimitGrace || a.Kind == core.TimerRunDeadline) {
			out = append(out, a)
		}
	}
	return out
}

// finishedWhenReached says the run's lead had given its final report when the run reached its limit:
// then it is stopped with no message.
func finishedWhenReached(w *script, run core.RunID) bool {
	for _, e := range w.log {
		if b, ok := e.Body.(core.Run); ok && b.ID == run && b.LimitReached != "" {
			return b.FinalReported
		}
	}
	return false
}
