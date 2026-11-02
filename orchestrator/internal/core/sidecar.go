package core

import (
	"cmp"
	"fmt"
	"maps"
	"slices"
)

// live returns the agent a fact is about, if that session is the current one and not over. A
// session that was written off, or replaced by a resume, has no more say.
func (t *tx) live(s Session) (agentState, bool) {
	a, ok := t.s.agents[s.Agent]
	if !ok || a.Session != s.N || a.Status.Over() {
		return agentState{}, false
	}
	return a, true
}

// fact applies one sidecar event. A repeat is at or below the committed offset, so a stream resumed
// after a crash changes nothing it already changed.
func (t *tx) fact(f SidecarFact) {
	a, ok := t.live(f.Session)
	if !ok || f.Offset <= a.offset {
		return
	}
	t.source = &SidecarSource{Session: f.Session, Offset: f.Offset}
	switch b := f.Body.(type) {
	case ConversationOpened:
		t.opened(a, b)
	case ItemObserved:
		t.item(a, b)
	case TurnEnded:
		t.turnEnded(a, b)
	case SteerOutcome:
		t.steerOutcome(a, b)
	case UsageReported:
		t.usage(a, b)
	case SessionFailed:
		t.sessionOver(a.ID, AgentFailed, b.Detail)
	case SessionClosed:
		t.sessionOver(a.ID, AgentEnded, "")
	default:
		panic(fmt.Sprintf("core: unknown fact %T", f.Body))
	}
	if len(t.events) > 0 {
		t.sweep(a.Run)
	}
}

func (t *tx) opened(a agentState, b ConversationOpened) {
	if a.Status != AgentLaunching {
		return
	}
	a.Status, a.ConversationID, a.Capabilities, a.Worktree = AgentIdle, b.ConversationID, b.Capabilities, b.Worktree
	t.emit(a.Run, a.Agent)
	if run := t.s.runs[a.Run]; a.Role == RoleLead && run.Status == RunStarting {
		run.Status = RunRunning
		t.emit(run.ID, run)
	}
}

func (t *tx) item(a agentState, b ItemObserved) {
	if a.turn == nil || a.turn.ID != b.Turn {
		return
	}
	item := Item{ID: b.ID, Turn: b.Turn, Agent: a.ID, Status: b.Status, At: t.in.At, Body: b.Body}
	if seen, ok := a.current[b.ID]; !ok {
		item.Ordinal = a.items + 1
	} else if seen.Status != ItemRunning {
		return
	} else {
		item.Ordinal = seen.Ordinal
	}
	t.emit(a.Run, item)
}

func openItems(a agentState) []Item {
	var out []Item
	for _, id := range slices.Sorted(maps.Keys(a.current)) {
		if item := a.current[id]; item.Status == ItemRunning {
			out = append(out, item)
		}
	}
	slices.SortFunc(out, func(x, y Item) int { return cmp.Compare(x.Ordinal, y.Ordinal) })
	return out
}

func (t *tx) turnEnded(a agentState, b TurnEnded) {
	if a.turn == nil || a.turn.ID != b.Turn || b.Status == TurnRunning {
		return
	}
	t.endTurn(a, b.Status, b.Usage)
	a = t.s.agents[a.ID]
	a.Status = AgentIdle
	if b.Status == TurnCompleted {
		a.SessionFailures = 0
	}
	t.emit(a.Run, a.Agent)
	done := a.Role != RoleLead && !a.HandedOff && a.Stop == StopNone && len(a.queue) == 0 && !t.steerPending(a.ID)
	if done && t.s.runs[a.Run].Status == RunRunning {
		t.notifyLead(a, AgentIdle, "")
	}
}

func (t *tx) steerOutcome(a agentState, b SteerOutcome) {
	m, ok := t.s.messages[b.Message]
	if !ok || m.To != a.ID || m.Delivery != DeliverySteered {
		return
	}
	switch {
	case b.Accepted:
		m.Delivery = DeliveryConsumed
	case a.Stop != StopNone:
		m.Delivery, m.Rejection = DeliveryRejected, RejectAgentStopping
	default:
		m.Delivery = DeliveryQueued
	}
	t.emit(a.Run, m)
}

func (t *tx) usage(a agentState, b UsageReported) {
	if a.turn == nil || a.turn.ID != b.Turn {
		return
	}
	turn := *a.turn
	turn.Usage = turn.Usage.merged(b.Usage)
	t.emit(a.Run, turn)
}

func (t *tx) lost(b SidecarLost) {
	a, ok := t.live(b.Session)
	if !ok {
		return
	}
	t.sessionOver(a.ID, AgentLost, b.Detail)
	t.sweep(a.Run)
}

// job tells the lead a job is over, once however often the feed repeats it. A job that is still
// going changes nothing here.
func (t *tx) job(b JobUpdate) {
	run, ok := t.s.runs[b.Run]
	if _, told := t.s.jobs[b.Job]; told || !ok || !b.State.Final() {
		return
	}
	t.post(Message{
		Run: run.ID, To: run.Lead,
		Body: JobFinished{Job: b.Job, Outcome: b.State, SubmittedBy: b.SubmittedBy, Detail: b.Detail},
	})
	t.sweep(run.ID)
}

func (t *tx) timer(b TimerFired) {
	switch b.Timer.Kind {
	case TimerRunDeadline:
		if run, ok := t.s.runs[b.Timer.Run]; ok && run.Status.Live() {
			t.beginStop(run, StopDeadline)
			t.sweep(run.ID)
		}
	case TimerStopGrace:
		if a, ok := t.live(b.Timer.Session); ok && a.Stop != StopNone {
			t.effect(CloseSession{Session: b.Timer.Session, Force: true})
			t.sessionOver(a.ID, AgentLost, "no answer to a stop")
			t.sweep(a.Run)
		}
	default:
		panic(fmt.Sprintf("core: unknown timer %q", b.Timer.Kind))
	}
}

// mail returns what is in transit to an agent, in id order.
func (t *tx) mail(id AgentID) []Message {
	var out []Message
	for _, msgID := range slices.Sorted(maps.Keys(t.s.messages)) {
		if m := t.s.messages[msgID]; m.To == id {
			out = append(out, m)
		}
	}
	return out
}
