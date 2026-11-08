package core

import (
	"fmt"
	"slices"
)

// Violations checks everything that must hold of a State, whatever led to it, and returns one line
// per breach. It is the machine-checked form of the guarantees in this package: the simulation
// calls it after every step, and a restart can call it after Replay.
func (s State) Violations() []string {
	var found []string
	bad := func(format string, args ...any) { found = append(found, fmt.Sprintf(format, args...)) }
	subagents := map[RunID]uint32{}
	for _, a := range s.agents {
		if a.Role != RoleLead && !a.Status.Over() {
			subagents[a.Run]++
		}
		if (a.turn != nil) != (a.Status == AgentActive) {
			bad("agent %s is %s with turn %v", a.ID, a.Status, a.turn)
		}
		if a.Status.Over() && (a.Stop != StopNone || len(a.queue) > 0) {
			bad("agent %s is over with stop %s and queue %v", a.ID, a.Stop, a.queue)
		}
		if a.Stop == StopNone && a.Status.Resting() && len(a.queue) > 0 && !s.briefWaits(a) {
			bad("agent %s rests with mail waiting: %v", a.ID, a.queue)
		}
		if run, ok := s.runs[a.Run]; ok && !slices.Contains(run.Allowed, AgentChoice{Harness: a.Spec.Harness, Model: a.Spec.Model}) {
			bad("agent %s runs on %s %q, outside what run %s allows: %v", a.ID, a.Spec.Harness, a.Spec.Model, run.ID, run.Allowed)
		}
	}
	for _, run := range s.runs {
		// Not here: whether a spawn or a resume was admitted at or over the fan-out limit. A run that an
		// update lowered the limit of has more live subagents than it, which is allowed, so no state says
		// whether an agent was let in wrongly; the simulation's monitor checks each admission from the log.
		s.leadChangeBreaches(run, bad)
		if run.FinalReported && !run.StalledSince.IsZero() {
			bad("run %s has given its final report and is stalled since %v", run.ID, run.StalledSince)
		}
		if stalled := s.stalled(run); stalled != !run.StalledSince.IsZero() {
			bad("run %s is %s with stalled since %v, but its stall is %t", run.ID, run.Status, run.StalledSince, stalled)
		}
		if s.finished(run) {
			bad("run %s gave its final report, rests with nothing to do and is still running", run.ID)
		}
		if len(run.Kept) > 0 && !run.Status.Final() {
			bad("run %s is %s and already keeps checkouts %v", run.ID, run.Status, run.Kept)
		}
		for _, c := range run.Kept {
			if a, ok := s.agents[c.Agent]; !ok || a.Run != run.ID || a.Role == RoleLead {
				bad("run %s keeps a checkout of %s, which is not one of its subagents", run.ID, c.Agent)
			}
		}
		for _, breach := range run.phaseBreaches(subagents[run.ID]) {
			bad("run %s %s", run.ID, breach)
		}
		for _, breach := range run.limitBreaches() {
			bad("run %s %s", run.ID, breach)
		}
		for _, breach := range run.summaryBreaches() {
			bad("run %s %s", run.ID, breach)
		}
		for _, breach := range s.checkInBreaches(run) {
			bad("run %s %s", run.ID, breach)
		}
		if run.LeadReminders > MaxLeadReminders {
			bad("run %s sent its lead %d reminders, over the limit of %d", run.ID, run.LeadReminders, MaxLeadReminders)
		}
		if run.Status.Final() && subagents[run.ID]+s.liveLead(run) > 0 {
			bad("run %s is %s with live agents", run.ID, run.Status)
		}
	}
	for _, m := range s.messages {
		a, ok := s.agents[m.To]
		if !ok {
			bad("message %s in transit to unknown agent %q", m.ID, m.To)
			continue
		}
		if a.Status.Over() {
			bad("message %s is %s for agent %s, which is over", m.ID, m.Delivery, a.ID)
		}
		if m.Delivery == DeliveryNewTurn && (a.turn == nil || a.turn.Message != m.ID) {
			bad("message %s started no running turn", m.ID)
		}
	}
	return found
}

// leadChangeBreaches says how a run's wait to change its lead disagrees with the run: it waits only in
// a run that is live, for a spec that the run allows and that is not the lead's unless the lead's session
// is already closing, and the lead of a live run stops only to be replaced.
func (s State) leadChangeBreaches(run Run, bad func(format string, args ...any)) {
	lead, ok := s.agents[run.Lead]
	// A lead whose brief is queued is replaced even by the spec it has: completing setup opens a new session.
	_, fresh := s.queuedBrief(lead)
	if run.PendingLead != nil {
		switch {
		case !run.Status.Live():
			bad("run %s is %s and still waits to change its lead to %+v", run.ID, run.Status, *run.PendingLead)
		case ok && lead.Spec == *run.PendingLead && lead.Stop == StopNone && !fresh:
			bad("run %s waits to change its lead to the spec it has: %+v", run.ID, lead.Spec)
		case !slices.Contains(run.Allowed, AgentChoice{Harness: run.PendingLead.Harness, Model: run.PendingLead.Model}):
			bad("run %s waits to change its lead to %+v, outside what it allows: %v", run.ID, *run.PendingLead, run.Allowed)
		}
	}
	stopping := lead.Stop == StopInterrupting || lead.Stop == StopClosing
	if ok && run.Status.Live() && stopping && run.PendingLead == nil {
		bad("the lead %s of live run %s is stopping and no other waits to take over", lead.ID, run.ID)
	}
}

func (s State) liveLead(run Run) uint32 {
	if s.agents[run.Lead].Status.Over() {
		return 0
	}
	return 1
}

// limitBreaches says how a run's limit fields disagree with each other and with what the run spent.
func (run Run) limitBreaches() []string {
	var found []string
	switch {
	case run.LimitReached == "" && !run.LimitReachedAt.IsZero():
		found = append(found, "reached no limit and has a time for it")
	case run.LimitReached != "" && run.LimitReachedAt.IsZero():
		found = append(found, "reached a limit with no time")
	case run.LimitReached == LimitTime && (run.Deadline.IsZero() || run.LimitReachedAt.Before(run.Deadline)):
		found = append(found, fmt.Sprintf("reached its time limit at %v, before its deadline %v", run.LimitReachedAt, run.Deadline))
	case run.LimitReached == LimitCost && (run.MaxCostUSD <= 0 || run.Usage.CostUSD == nil || *run.Usage.CostUSD < run.MaxCostUSD):
		found = append(found, fmt.Sprintf("reached its cost limit of %v with usage %v", run.MaxCostUSD, run.Usage.CostUSD))
	}
	limited := run.StopReason == StopTimeLimit || run.StopReason == StopCostLimit
	if limited && (run.LimitReached == "" || run.StopReason != run.LimitReached.stopReason()) {
		found = append(found, fmt.Sprintf("stopped for %s having reached %q", run.StopReason, run.LimitReached))
	}
	return found
}

// summaryBreaches says how a run's report schedule disagrees with the run: a request is waiting only
// for a run that has a schedule, can be asked, and whose request is not due before the interval has
// passed.
func (run Run) summaryBreaches() []string {
	if run.NextSummaryAt.IsZero() {
		return nil
	}
	var found []string
	if run.SummaryEvery <= 0 {
		found = append(found, "has a request for a report due and no schedule")
	}
	if !run.asksForReports() {
		found = append(found, fmt.Sprintf("has a request for a report due at %v while it is %s, has reached %q and its final report stands: %t",
			run.NextSummaryAt, run.Status, run.LimitReached, run.FinalReported))
	}
	if run.NextSummaryAt.Before(run.LastSummaryAt.Add(run.SummaryEvery)) {
		found = append(found, fmt.Sprintf("is to be asked for a report at %v, before %v, the interval after %v",
			run.NextSummaryAt, run.LastSummaryAt.Add(run.SummaryEvery), run.LastSummaryAt))
	}
	return found
}

// phaseBreaches says how a run's phase disagrees with the rest of it. A run in setup has no subagent, no
// task, no limit, no schedule and no setup account; the setup account of a run that left setup is a part
// of its usage, and exists only once the run has an optimize start to be apart from.
func (run Run) phaseBreaches(subagents uint32) []string {
	var found []string
	if run.SetupUsage != nil {
		if run.OptimizeStartedAt.IsZero() {
			found = append(found, "has a setup account and never left setup")
		}
		if !run.SetupUsage.within(run.Usage) {
			found = append(found, fmt.Sprintf("spent %+v in setup, more than its usage %+v", *run.SetupUsage, run.Usage))
		}
	}
	if !run.inSetup() {
		return found
	}
	if subagents > 0 {
		found = append(found, fmt.Sprintf("is in setup with %d live subagents", subagents))
	}
	if run.Task != "" || !run.OptimizeStartedAt.IsZero() || run.SetupUsage != nil {
		found = append(found, fmt.Sprintf("is in setup with task %q, optimize start %v and setup usage %v", run.Task, run.OptimizeStartedAt, run.SetupUsage))
	}
	if !run.Deadline.IsZero() || run.MaxCostUSD != 0 || run.LimitReached != "" || run.FinalReported || !run.NextSummaryAt.IsZero() {
		found = append(found, "is in setup with a limit, a final report or a report due")
	}
	return found
}
