package core

import "slices"

// This file holds the swap from setup to optimize: the operator completes setup, and the run starts
// optimizing the task its setup made. One decision commits all of it, so that a log cut anywhere holds
// the swap whole or not at all:
//
//   - the phase, the task, its card and eval place, the limits, when optimizing began, the setup's
//     usage, the deadline (with its timer) and the start of the summary schedule;
//   - the lead the run is to have, always a fresh session on the chosen spec even when the spec is the
//     one it has (PendingLead), because the optimize lead must not carry the setup conversation;
//   - the queued LeadBrief, the optimize lead's first message.
//
// Nothing else is done here. The lead is never cut: a start while it is mid-turn waits for it to rest,
// as a change of lead does, and what the turn spends is setup spend (rollUp counts a turn by when it
// started). Once it rests, handover.go closes its session, opens the new one on the spec and the new
// lead reads the brief first and once. The swap is taken from the state at each of those steps, so a
// crash between them recovers into the next one.

// unmet are the checks the verdict does not hold: the ones it names, and all six when it is a verdict
// about another task than the one completed, or about none (see SetupVerdict).
func (v SetupVerdict) unmet(task string) []SetupCheckID {
	if v.Task != task {
		return slices.Clone(SetupChecks)
	}
	return v.Unmet
}

// completeSetup swaps a run in setup to optimize.
func (t *tx) completeSetup(b CompleteSetup, r *Receipt) RunID {
	run, ok := t.s.runs[b.Run]
	switch {
	case !ok:
		r.Refusal = RefuseRunNotFound
		return ""
	case !run.inSetup():
		// Any run that is not in setup: one that began in optimize, one recorded before phases, or one
		// whose setup is already complete.
		r.Run, r.Refusal = run.ID, RefuseNotInSetup
		return ""
	case !run.Status.Live():
		r.Run, r.Refusal = run.ID, RefuseRunNotRunning
		return ""
	}
	r.Run = run.ID
	if unmet := b.Verified.unmet(b.Task); len(unmet) > 0 {
		r.Refusal, r.UnmetChecks = RefuseSetupIncomplete, unmet
		return ""
	}
	facts := resolvedFacts(b.Verified.Facts, b.EvalPlace)
	if facts.Refused != nil {
		r.Refusal, r.EvalPlaces = facts.Refused.Refusal, facts.Refused.Places
		return ""
	}
	model, ok := t.resolveChoice(r, b.Lead.Harness, b.Lead.Model)
	if !ok {
		return ""
	}
	lead := b.Lead
	lead.Model = model

	setupUsage := run.Usage
	run.Phase, run.Task, run.SetupTask = PhaseOptimize, b.Task, ""
	run.TaskCard, run.EvalPlace = facts.Card, facts.EvalPlace
	run.FanoutLimit, run.MaxCostUSD, run.SummaryEvery = b.FanoutLimit, b.MaxCostUSD, DefaultSummaryEvery
	run.OptimizeStartedAt, run.SetupUsage, run.LastSummaryAt = t.in.At, &setupUsage, t.in.At
	if b.MaxTime > 0 {
		run.Deadline = t.in.At.Add(b.MaxTime)
	}
	run.PendingLead = &lead
	if choice := (AgentChoice{Harness: lead.Harness, Model: lead.Model}); !slices.Contains(run.Allowed, choice) {
		run.Allowed = append(slices.Clone(run.Allowed), choice)
	}
	r.Agent = run.Lead
	t.emit(run.ID, run)
	if !run.Deadline.IsZero() {
		t.effect(ArmTimer{Kind: TimerRunDeadline, Run: run.ID, FireAt: run.Deadline})
	}
	if stale, unread := t.queuedHandover(t.s.agents[run.Lead]); unread {
		// A handover of an earlier change of lead that its session has not read is replaced by the brief,
		// which tells the next lead the whole run: the two are never queued together.
		old := t.s.messages[stale]
		old.Delivery = DeliveryConsumed
		t.emit(run.ID, old)
	}
	r.Message = t.post(Message{Run: run.ID, To: run.Lead, Body: LeadBrief{Task: b.Task, FanoutLimit: b.FanoutLimit, Steering: b.Brief}}).ID
	return run.ID
}

// briefWaits says the lead holds the brief of a swap, for the session that takes over from it.
func (s State) briefWaits(a agentState) bool {
	_, held := s.queuedBrief(a)
	return held && a.Role == RoleLead && s.runs[a.Run].PendingLead != nil
}

// briefsNextSession says the message is the brief that completing setup sent the lead of a run whose
// lead is to be replaced. It is for the session that opens next, so it waits whatever the lead is doing
// now, a turn that steers included: the lead's turn is never cut, and the brief is not read in it.
func (t *tx) briefsNextSession(a agentState, m Message) bool {
	if _, brief := m.Body.(LeadBrief); !brief {
		return false
	}
	run := t.s.runs[a.Run]
	return a.Role == RoleLead && run.Status.Live() && run.PendingLead != nil
}
