package core

import "fmt"

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
	}
	for _, run := range s.runs {
		if subagents[run.ID] > run.FanoutLimit {
			bad("run %s has %d live subagents over its limit of %d", run.ID, subagents[run.ID], run.FanoutLimit)
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
