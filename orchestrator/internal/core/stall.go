package core

import "time"

// This file holds the rules for a run that stands still and for an agent that stops without a
// handoff. Each is a small function over the transaction, called from the places that change what
// it reads: sweep for the stall, a turn's end for the handoff reminder, a timer for the lead's.

// stalled says nothing is left that could move the run: it is running, its lead rests with nothing
// to do, no other agent is launching or working, and no job is queued or running. Such a run stays
// as it is until an operator acts, whatever the lead meant to do next. An agent that is idle
// without a handoff does not count as working: the lead has been told of it.
func (s State) stalled(run Run) bool {
	if run.Status != RunRunning || run.ActiveJobs > 0 {
		return false
	}
	for _, m := range s.messages {
		if m.Run == run.ID && m.To != "" {
			return false
		}
	}
	for _, a := range s.agents {
		if a.Run != run.ID || a.Status.Over() {
			continue
		}
		if a.Stop != StopNone || len(a.queue) > 0 || !a.Status.Resting() {
			return false
		}
	}
	return s.agents[run.Lead].Status.Resting()
}

// markStall keeps the run's StalledSince true to stalled. The time is when the stall began, and a
// stall that begins arms the timer that reminds the lead.
func (t *tx) markStall(id RunID) {
	run := t.s.runs[id]
	switch stalled := t.s.stalled(run); {
	case stalled && run.StalledSince.IsZero():
		run.StalledSince = t.in.At
		t.emit(id, run)
		if run.LeadReminders < MaxLeadReminders {
			t.effect(ArmTimer{Kind: TimerLeadIdle, Run: id, FireAt: t.in.At.Add(LeadIdleAfter)})
		}
	case !stalled && !run.StalledSince.IsZero():
		run.StalledSince = time.Time{}
		t.emit(id, run)
	}
}

// leadIdle answers the timer armed when a stall began. It reminds the lead once for that stall,
// while the stall is the one the timer was armed for, and no more than MaxLeadReminders times in a
// run. Nothing ends the run: a lead that stays quiet leaves a stall anyone can see.
func (t *tx) leadIdle(b TimerFired) {
	run, ok := t.s.runs[b.Timer.Run]
	if !ok || run.StalledSince.IsZero() || run.LeadReminders >= MaxLeadReminders ||
		!run.StalledSince.Add(LeadIdleAfter).Equal(b.Timer.FireAt) || !t.s.stalled(run) {
		return
	}
	run.LeadReminders++
	t.emit(run.ID, run)
	t.post(Message{Run: run.ID, To: run.Lead, Body: LeadIdleReminder{Idle: t.in.At.Sub(run.StalledSince).Truncate(time.Second)}})
	t.sweep(run.ID)
}

// jobsRunning records how many jobs of a run are going, and moves the stall with it.
func (t *tx) jobsRunning(b JobsRunning) {
	run, ok := t.s.runs[b.Run]
	if !ok || run.ActiveJobs == b.Count || !run.Status.Live() {
		return
	}
	run.ActiveJobs = b.Count
	t.emit(run.ID, run)
	t.sweep(run.ID)
}

// settled is what a turn's end does for an agent that is not the lead, did not hand off and has
// nothing waiting: it asks the agent once per session to post its handoff, and tells the lead when
// the agent settles again.
func (t *tx) settled(a agentState) {
	if t.s.runs[a.Run].Status != RunRunning {
		return
	}
	if a.HandoffReminded {
		t.notifyLead(a, AgentIdle, "")
		return
	}
	a.HandoffReminded = true
	t.emit(a.Run, a.Agent)
	t.post(Message{Run: a.Run, To: a.ID, Body: HandoffReminder{}})
}
