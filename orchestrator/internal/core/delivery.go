package core

import "time"

// post routes a new message. It is the one way a message enters the system.
func (t *tx) post(m Message) Message {
	m.ID, m.At = MessageID(t.mint("message")), t.in.At
	run := t.s.runs[m.Run]
	switch {
	case !run.Status.Live():
		m.Delivery, m.Rejection = DeliveryRejected, RejectRunStopping
	case m.To == "":
		m.Delivery = DeliveryConsumed
	default:
		t.route(&m, t.s.agents[m.To])
	}
	t.emit(m.Run, m)
	return m
}

// route picks a delivery by what the agent is doing and what it can do. A message that may start
// a turn is queued: sweep starts one turn at a time, so no path here can start a second.
func (t *tx) route(m *Message, a agentState) {
	switch {
	case a.Stop != StopNone:
		m.Delivery, m.Rejection = DeliveryRejected, RejectAgentStopping
	case a.Status == AgentActive && a.Capabilities.Steers:
		m.Delivery = DeliverySteered
		t.effect(Steer{Session: Session{a.ID, a.Session}, Turn: a.turn.ID, Message: *m})
	case !a.Status.Over():
		m.Delivery = DeliveryQueued
	case !a.Capabilities.Resumes || a.ConversationID == "":
		m.Delivery, m.Rejection = DeliveryRejected, RejectAgentGone
	case a.Role != RoleLead && t.liveSubagents(m.Run) >= t.s.runs[m.Run].FanoutLimit:
		m.Delivery, m.Rejection = DeliveryRejected, RejectFanoutLimit
	default:
		m.Delivery = DeliveryQueued
		t.resume(a)
	}
}

// resume opens a new session on an agent whose last one is over.
func (t *tx) resume(a agentState) {
	a.Status, a.Stop, a.Session, a.EndedAt, a.HandedOff, a.EndDetail, a.StartFailure = AgentLaunching, StopNone, a.Session+1, time.Time{}, false, "", ""
	a.HandoffReminded = false
	t.emit(a.Run, a.Agent)
	run := t.s.runs[a.Run]
	t.effect(LaunchSession{
		Session: Session{a.ID, a.Session}, Run: run.ID, Repository: run.Repository, Task: run.Task, Role: a.Role, Spec: a.Spec,
		Location: run.Location, ConversationID: a.ConversationID, Worktree: a.Worktree,
	})
	t.armLaunchDeadline(run.ID, Session{a.ID, a.Session})
}

// sweep brings a run to rest after a change: it starts the turn each agent is due, closes the
// sessions that are done, and finishes the run when it can. Every path that changes an agent ends
// here, so these rules hold on all of them.
func (t *tx) sweep(id RunID) {
	for _, a := range t.sortedAgents(id) {
		switch {
		case a.Status.Over():
			continue
		case a.Stop == StopInterrupting && a.turn == nil:
			t.closeSession(a)
		case a.Stop != StopNone || !a.Status.Resting():
			continue
		case len(a.queue) > 0:
			t.startTurn(a)
		case a.HandedOff && !t.steerPending(a.ID):
			t.stopAgent(a.ID)
		default:
			t.restStatus(a)
		}
	}
	t.markStall(id)
	t.finishRun(id)
}

func (t *tx) startTurn(a agentState) {
	m := t.s.messages[a.queue[0]]
	m.Delivery = DeliveryNewTurn
	m.Attempts++
	t.emit(a.Run, m)
	turn := Turn{
		ID: TurnID(t.mint("turn")), Agent: a.ID, Ordinal: a.turns + 1, Trigger: m.trigger(), Message: m.ID,
		Status: TurnRunning, StartedAt: t.in.At,
	}
	t.emit(a.Run, turn)
	a = t.s.agents[a.ID]
	a.Status, a.HandedOff = AgentActive, false
	t.emit(a.Run, a.Agent)
	if run := t.s.runs[a.Run]; run.FinalReported && run.Lead == a.ID {
		// New input for a lead that had finished: it goes on, and the run is judged as any other.
		run.FinalReported = false
		t.emit(run.ID, run)
	}
	t.effect(StartTurn{Session: Session{a.ID, a.Session}, Turn: turn.ID, Message: m})
}

// restStatus keeps a resting agent's status true to whether it has live subagents.
func (t *tx) restStatus(a agentState) {
	want := AgentIdle
	for _, child := range t.s.agents {
		if child.Parent == a.ID && !child.Status.Over() {
			want = AgentWaiting
			break
		}
	}
	if a.Status != want {
		a.Status = want
		t.emit(a.Run, a.Agent)
	}
}

func (t *tx) steerPending(id AgentID) bool {
	for _, m := range t.s.messages {
		if m.To == id && m.Delivery == DeliverySteered {
			return true
		}
	}
	return false
}

// stopAgent starts stopping a live agent: an agent in a turn is interrupted first. The grace timer
// is what ends the wait if its session never answers.
func (t *tx) stopAgent(id AgentID) {
	a := t.s.agents[id]
	if a.Status.Over() || a.Stop != StopNone {
		return
	}
	session := Session{a.ID, a.Session}
	t.effect(ArmTimer{Kind: TimerStopGrace, Run: a.Run, Session: session, FireAt: t.in.At.Add(StopGrace)})
	if a.turn == nil {
		t.closeSession(a)
		return
	}
	a.Stop = StopInterrupting
	t.emit(a.Run, a.Agent)
	t.effect(Interrupt{Session: session, Turn: a.turn.ID})
}

func (t *tx) closeSession(a agentState) {
	a.Stop = StopClosing
	t.emit(a.Run, a.Agent)
	t.effect(CloseSession{Session: Session{a.ID, a.Session}})
}

// beginStop moves a live run to stopping and stops its agents.
func (t *tx) beginStop(run Run, reason RunStopReason) {
	run.Status, run.StopReason = RunStopping, reason
	t.emit(run.ID, run)
	for _, a := range t.sortedAgents(run.ID) {
		t.stopAgent(a.ID)
	}
}

// finishRun ends a stopping run when its last agent is over. The status is derived from why it
// stopped, so nothing can finish a run as a success that a lost lead ended.
func (t *tx) finishRun(id RunID) {
	run := t.s.runs[id]
	if run.Status != RunStopping {
		return
	}
	for _, a := range t.s.agents {
		if a.Run == id && !a.Status.Over() {
			return
		}
	}
	run.Status, run.EndedAt = RunEnded, t.in.At
	if run.StopReason == StopLeadGone {
		run.Status = RunFailed
	}
	t.emit(id, run)
}

// endTurn closes a turn, and first everything inside it, so that no reader sees a finished turn
// with a running item. A message whose turn did not complete was not delivered and goes back in
// line, until its attempts run out.
func (t *tx) endTurn(a agentState, status TurnStatus, usage Usage) {
	itemStatus := ItemFailed
	if status == TurnCompleted {
		itemStatus = ItemFinished
	}
	for _, item := range openItems(a) {
		item.Status, item.At = itemStatus, t.in.At
		t.emit(a.Run, item)
	}
	turn := *a.turn
	turn.Status, turn.EndedAt, turn.Usage = status, t.in.At, turn.Usage.merged(usage)
	t.emit(a.Run, turn)
	t.rollUp(a, turn.Usage)

	m := t.s.messages[turn.Message]
	switch {
	case status == TurnCompleted:
		m.Delivery = DeliveryConsumed
	case m.Attempts >= MaxDeliveryAttempts:
		m.Delivery, m.Rejection = DeliveryRejected, RejectAttemptsExceeded
	default:
		m.Delivery = DeliveryQueued
	}
	t.emit(a.Run, m)
}

// rollUp adds a finished turn's cost to its agent's and its run's. A turn that reported nothing
// adds nothing and writes nothing.
func (t *tx) rollUp(a agentState, u Usage) {
	if u == (Usage{}) {
		return
	}
	a = t.s.agents[a.ID]
	a.Usage = a.Usage.plus(u)
	t.emit(a.Run, a.Agent)
	run := t.s.runs[a.Run]
	run.Usage = run.Usage.plus(u)
	t.emit(run.ID, run)
}

// sessionOver records the end of a session. The lead of a live run comes back if it can, because a
// run has no use without one; any other agent stays down and its lead hears of it.
//
// A session that ends while it is still launching never opened, and records why: the reason it was
// given, else StartOther. One that had opened records none.
func (t *tx) sessionOver(id AgentID, status AgentStatus, detail string, start StartFailure) {
	a := t.s.agents[id]
	if a.Status != AgentLaunching {
		start = ""
	} else if start == "" && status != AgentEnded {
		start = StartOther
	}
	unasked := a.Stop == StopNone
	if a.turn != nil {
		turnStatus := TurnFailed
		if status == AgentEnded {
			turnStatus = TurnInterrupted
		}
		t.endTurn(a, turnStatus, Usage{})
		a = t.s.agents[id]
	}
	a.Status, a.Stop, a.EndedAt, a.EndDetail, a.StartFailure = status, StopNone, t.in.At, detail, start
	if unasked {
		a.SessionFailures++
	}
	t.emit(a.Run, a.Agent)

	run := t.s.runs[a.Run]
	if a.Role == RoleLead && run.Status.Live() {
		if a.Capabilities.Resumes && a.ConversationID != "" && a.SessionFailures <= MaxLeadResumes {
			t.requeueSteers(a)
			t.resume(t.s.agents[id])
			return
		}
		t.rejectMail(a.ID)
		t.beginStop(run, StopLeadGone)
		return
	}
	t.rejectMail(a.ID)
	if a.Role != RoleLead && !a.HandedOff && run.Status == RunRunning {
		t.notifyLead(a, status, detail)
	}
}

// notifyLead tells the lead a subagent stopped working without a handoff, so that a lead waiting
// for that handoff always has something to wake it.
func (t *tx) notifyLead(a agentState, status AgentStatus, detail string) {
	run := t.s.runs[a.Run]
	t.post(Message{Run: a.Run, To: run.Lead, Body: SubagentSettled{Agent: a.ID, Status: status, Detail: detail}})
}

// requeueSteers puts back steers whose session ended before it answered. The message may have
// landed, so across a lost session delivery is at least once.
func (t *tx) requeueSteers(a agentState) {
	for _, m := range t.mail(a.ID) {
		if m.Delivery == DeliverySteered {
			m.Delivery = DeliveryQueued
			t.emit(a.Run, m)
		}
	}
}

// rejectMail settles what an agent that is staying down was still owed.
func (t *tx) rejectMail(id AgentID) {
	reason := RejectAgentGone
	a := t.s.agents[id]
	if !t.s.runs[a.Run].Status.Live() {
		reason = RejectRunStopping
	}
	for _, m := range t.mail(id) {
		m.Delivery, m.Rejection = DeliveryRejected, reason
		t.emit(a.Run, m)
	}
}
