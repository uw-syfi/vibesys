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
		if a.Stop == StopNone && a.Status.Resting() && len(a.queue) > 0 {
			bad("agent %s rests with mail waiting: %v", a.ID, a.queue)
		}
		if run, ok := s.runs[a.Run]; ok && !slices.Contains(run.Allowed, AgentChoice{Harness: a.Spec.Harness, Model: a.Spec.Model}) {
			bad("agent %s runs on %s %q, outside what run %s allows: %v", a.ID, a.Spec.Harness, a.Spec.Model, run.ID, run.Allowed)
		}
	}
	for _, run := range s.runs {
		if subagents[run.ID] > run.FanoutLimit {
			bad("run %s has %d live subagents over its limit of %d", run.ID, subagents[run.ID], run.FanoutLimit)
		}
		if run.FinalReported && !run.StalledSince.IsZero() {
			bad("run %s has given its final report and is stalled since %v", run.ID, run.StalledSince)
		}
		if stalled := s.stalled(run); stalled != !run.StalledSince.IsZero() {
			bad("run %s is %s with stalled since %v, but its stall is %t", run.ID, run.Status, run.StalledSince, stalled)
		}
		if len(run.Kept) > 0 && !run.Status.Final() {
			bad("run %s is %s and already keeps checkouts %v", run.ID, run.Status, run.Kept)
		}
		for _, c := range run.Kept {
			if a, ok := s.agents[c.Agent]; !ok || a.Run != run.ID || a.Role == RoleLead {
				bad("run %s keeps a checkout of %s, which is not one of its subagents", run.ID, c.Agent)
			}
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

func (s State) liveLead(run Run) uint32 {
	if s.agents[run.Lead].Status.Over() {
		return 0
	}
	return 1
}
