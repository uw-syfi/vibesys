package core

import "time"

// This file holds the limits of a run: what happens when one is reached.
//
// A time limit is the run's Deadline, reached by the timer armed at its start. A cost limit is
// reached when a turn's end brings the run's cost, as its finished turns reported it, to MaxCostUSD,
// so a run can pass it by what was in flight. Reaching a limit tells the lead once to finish, turns
// away every further spawn, and gives the lead LimitGrace; the run stops when the lead gives its
// final report (see postReport) or when the grace has passed. A lead that had already given its
// final report is not told: the run stops at once.

// costLimit reaches the cost limit when the run's finished turns have cost the most it may.
func (t *tx) costLimit(id RunID) {
	run := t.s.runs[id]
	// What counts is what the optimize phase spent: setup is accounted apart (setup_account.go) and a run
	// in setup has no limit.
	if spent := run.optimizeUsage().CostUSD; run.Phase.Optimizing() && run.MaxCostUSD > 0 && spent != nil && *spent >= run.MaxCostUSD {
		t.reachLimit(id, LimitCost)
	}
}

// reachLimit records that a live run reached a limit, once. The first limit a run reaches is the one
// it is stopped for.
func (t *tx) reachLimit(id RunID, limit RunLimit) {
	run, ok := t.s.runs[id]
	if !ok || !run.Status.Live() || run.LimitReached != "" {
		return
	}
	run.LimitReached, run.LimitReachedAt = limit, t.in.At
	run = run.unscheduled()
	t.emit(id, run)
	if run.FinalReported {
		t.beginStop(t.s.runs[id], limit.stopReason())
		return
	}
	// The message carries whole seconds, as the wire does, so the log replays the message that was
	// posted: a cost limit is reached at whatever instant a turn's end arrives.
	t.post(Message{Run: id, To: run.Lead, Body: RunLimitReached{
		Limit: limit, Elapsed: t.in.At.Sub(run.limitClock()).Truncate(time.Second), Spent: run.optimizeUsage().CostUSD, MaxCost: run.MaxCostUSD, Grace: LimitGrace,
	}})
	t.effect(ArmTimer{Kind: TimerLimitGrace, Run: id, FireAt: t.in.At.Add(LimitGrace)})
}
