package core

import "time"

// This file holds the change of a run's lead: the operator asks for another tool or model (see
// wantLead), and the run's lead is replaced by a new session of the same agent on that spec.
//
// The wanted spec is Run.PendingLead, which is durable, so the change is in the log from the moment it
// is asked and a restart in the middle of it finds it. It then goes in two steps, each taken from the
// state and so each recovered by the next input of the run:
//
//   - beginHandover, from sweep, when the lead is at rest: nothing is running, nothing it sent is
//     unanswered and it holds no handover it has not read. The lead's session is closed.
//   - handOver, from sessionOver, when that session is over, however it ended (the close was answered,
//     the grace passed, or the session fell on its own while a change waited): a fresh session opens on
//     the new spec with no conversation to continue, the change is recorded as applied, and a
//     LeadHandover is queued ahead of the lead's other mail.
//
// The new session is launched only after the old one is over, so a run never has two lead sessions.
// The lead's agent id, its mail and its queue are the same across the change: what arrived while the
// session was closing is queued, never rejected, and the new lead reads the handover first and the
// rest after. A run that begins to stop first drops the change.

// beingReplaced says the agent is the lead of a live run, whose session is closing so that the lead
// the operator asked for can take over. Nothing else stops the lead of a live run.
func (t *tx) beingReplaced(a agentState) bool {
	run := t.s.runs[a.Run]
	return a.Role == RoleLead && a.Stop != StopNone && run.Status.Live() && run.PendingLead != nil
}

// handoverDue says the lead rests where it can be replaced. A lead that has not yet read the handover
// of an earlier change waits for it: one handover is read before the next is made.
func (t *tx) handoverDue(a agentState) bool {
	run := t.s.runs[a.Run]
	if a.Role != RoleLead || !run.Status.Live() || run.PendingLead == nil || t.steerPending(a.ID) {
		return false
	}
	_, unread := t.queuedHandover(a)
	return !unread
}

// queuedHandover finds the handover an agent has been sent and not yet started a turn on.
func (t *tx) queuedHandover(a agentState) (MessageID, bool) {
	for _, id := range a.queue {
		if _, held := t.s.messages[id].Body.(LeadHandover); held {
			return id, true
		}
	}
	return "", false
}

// beginHandover closes the session of a lead that is due to be replaced. The grace timer ends the wait
// if the session never answers.
func (t *tx) beginHandover(a agentState) { t.stopAgent(a.ID) }

// handOver opens the new lead's session, after the old one is over.
func (t *tx) handOver(a agentState) {
	run := t.s.runs[a.Run]
	to := *run.PendingLead
	handover := t.handoverFor(run, a)
	run.PendingLead = nil
	t.emit(run.ID, run)
	// A fresh session: it has shown no failures and no capabilities yet, and has no token to resume.
	a.SessionFailures, a.Capabilities = 0, Capabilities{}
	t.reopen(a, to, "")
	t.emit(run.ID, RunSettingsUpdated{Run: run.ID, Changes: []SettingChange{
		{Lead: &LeadChange{From: handover.Previous, To: to, State: LeadChangeApplied}},
	}})
	// A lead that fell before it read the handover of an earlier change is handed the run as it stands now
	// in the same message: one handover is read, never two.
	unread, _ := t.queuedHandover(a)
	t.post(Message{ID: unread, Run: run.ID, To: a.ID, Body: handover})
}

// handoverFor words what the new lead is told, from the run as it stands: nothing in it is read from
// anywhere but the state.
func (t *tx) handoverFor(run Run, lead agentState) LeadHandover {
	h := LeadHandover{
		Task: run.Task, FanoutLimit: run.FanoutLimit, Previous: lead.Spec,
		// Whole seconds, as the wire holds them, so the log replays the message that was posted.
		Elapsed: t.in.At.Sub(run.limitClock()).Truncate(time.Second), Spent: run.optimizeUsage().CostUSD, MaxCost: run.MaxCostUSD,
		LimitReached: run.LimitReached, ReportsPosted: run.ReportsPosted, ActiveJobs: run.ActiveJobs,
	}
	if total := run.totalTime(); total > 0 {
		h.MaxTime = max(time.Second, total.Truncate(time.Second))
	}
	if report, ok := t.s.reports[run.ID]; ok {
		h.LatestReport = &report
	}
	for _, a := range t.sortedAgents(run.ID) {
		switch {
		case a.Role == RoleLead:
		case a.Status.Over():
			h.AgentsEnded++
		default:
			h.Agents = append(h.Agents, HandoverAgent{ID: a.ID, Role: a.Role, Status: a.Status, PlanNumber: a.PlanNumber})
		}
	}
	return h
}
