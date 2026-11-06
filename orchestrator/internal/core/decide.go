package core

import (
	"fmt"
	"maps"
	"slices"
	"strconv"
)

// Decide takes one input to the state after it, the events that say what changed, and the effects
// to carry out. It does not change s. Replay(s, events) equals the state it returns.
//
// An input that means nothing now (a repeated fact, a stale timer, a repeated command) returns s
// with no events and no effects. Decide does not fail: a command it will not carry out gets a
// receipt with a refusal, and an InputBody outside the closed set is a bug, which panics.
func Decide(s State, in Input) (State, []Event, []Effect) {
	t := &tx{s: s, in: in, delivery: map[MessageID]Delivery{}}
	switch b := in.Body.(type) {
	case Command:
		t.command(b)
	case SessionFact:
		t.fact(b)
	case SessionLost:
		t.lost(b)
	case JobUpdate:
		t.job(b)
	case JobsRunning:
		t.jobsRunning(b)
	case TimerFired:
		t.timer(b)
	default:
		panic(fmt.Sprintf("core: unknown input %T", in.Body))
	}
	if len(t.events) == 0 {
		return s, nil, nil
	}
	return t.s, t.events, t.effects
}

// tx is one Decide in progress.
type tx struct {
	s       State
	written written
	in      Input
	// command and source fill the envelope of every event.
	commandID CommandID
	source    *SessionSource
	events    []Event
	effects   []Effect
	minted    int
	// delivery follows the last delivery of each message written here, which outlives the message
	// if it is settled.
	delivery map[MessageID]Delivery
}

// emit commits one entity to the state and the event list.
func (t *tx) emit(run RunID, body EventBody) {
	e := Event{Run: run, Command: t.commandID, At: t.in.At, Source: t.source, Body: body}
	t.s.apply(e, &t.written)
	t.events = append(t.events, e)
	if m, ok := body.(Message); ok {
		t.delivery[m.ID] = m.Delivery
	}
}

func (t *tx) effect(e Effect) { t.effects = append(t.effects, e) }

// mint returns an id no other Decide returns, given a fresh nonce.
func (t *tx) mint(kind string) string {
	t.minted++
	return kind + "-" + string(t.in.Nonce) + "-" + strconv.Itoa(t.minted)
}

// command runs one command to its receipt.
func (t *tx) command(c Command) {
	if _, seen := t.s.receipts[c.ID]; seen {
		return
	}
	t.commandID = c.ID
	r := Receipt{Command: c.ID, Digest: c.Digest}
	var run RunID
	if refusal := t.permit(c); refusal != "" {
		r.Refusal = refusal
	} else {
		switch b := c.Body.(type) {
		case StartRun:
			run = t.startRun(c, b, &r)
		case StopRun:
			run = t.stopRun(b, &r)
		case SpawnAgent:
			run = t.spawnAgent(c, b, &r)
		case StopAgent:
			run = t.stopSubagent(c, b, &r)
		case SendMessage:
			run = t.sendMessage(c, b, &r)
		case PostHandoff:
			run = t.postHandoff(c, b, &r)
		case PostReport:
			run = t.postReport(c, b, &r)
		default:
			panic(fmt.Sprintf("core: unknown command %T", c.Body))
		}
	}
	if run != "" {
		t.sweep(run)
	}
	if r.Message != "" {
		r.Delivery = t.delivery[r.Message]
	}
	t.emit(r.Run, r)
}

// permit applies the role table, and holds a caller that is an agent to the live agents.
func (t *tx) permit(c Command) Refusal {
	var p Principal
	switch caller := c.Caller.(type) {
	case Operator:
		p = PrincipalOperator
	case AgentCaller:
		a, ok := t.s.agents[caller.Agent]
		if !ok || a.Status.Over() {
			return RefuseNotPermitted
		}
		p = Principal(a.Role)
	default:
		return RefuseNotPermitted
	}
	if !Allowed(p, c.Body.action()) {
		return RefuseNotPermitted
	}
	return ""
}

// callerIn says the caller may act on this run: an agent acts only in its own.
func (t *tx) callerIn(c Command, run RunID) bool {
	caller, ok := c.Caller.(AgentCaller)
	return !ok || t.s.agents[caller.Agent].Run == run
}

func callerAgent(c Command) AgentID {
	if caller, ok := c.Caller.(AgentCaller); ok {
		return caller.Agent
	}
	return ""
}

func (t *tx) startRun(c Command, b StartRun, r *Receipt) RunID {
	if !b.Deadline.IsZero() && !b.Deadline.After(t.in.At) {
		r.Refusal = RefuseDeadlinePassed
		return ""
	}
	if busy, ok := t.runInRepository(b.Repository); ok {
		r.Run, r.Refusal = busy, RefuseRepositoryInUse
		return ""
	}
	run := Run{
		ID: RunID(t.mint("run")), Repository: b.Repository, Task: b.Task, Status: RunStarting,
		Lead: AgentID(t.mint("agent")), FanoutLimit: b.FanoutLimit, Deadline: b.Deadline, CreatedAt: t.in.At,
		Location: t.in.Location,
	}
	r.Run, r.Agent = run.ID, run.Lead
	t.emit(run.ID, run)
	if !run.Deadline.IsZero() {
		t.effect(ArmTimer{Kind: TimerRunDeadline, Run: run.ID, FireAt: run.Deadline})
	}
	t.launch(run, Agent{ID: run.Lead, Run: run.ID, Role: RoleLead, Spec: b.Lead})
	r.Message = t.post(Message{Run: run.ID, To: run.Lead, Body: LeadBrief{Task: b.Task, FanoutLimit: b.FanoutLimit, Steering: b.Brief}}).ID
	return run.ID
}

// runInRepository returns the oldest run that is not over and works in repository. Two runs in one
// repository would share one checkout, so a start is refused while one is there, a run that is still
// stopping included. Only a start is held to this: a log that already holds two such runs replays as
// it was written.
func (t *tx) runInRepository(repository string) (RunID, bool) {
	var found Run
	for _, run := range t.s.runs {
		if run.Repository != repository || run.Status.Final() {
			continue
		}
		if found.ID == "" || run.CreatedAt.Before(found.CreatedAt) || run.CreatedAt.Equal(found.CreatedAt) && run.ID < found.ID {
			found = run
		}
	}
	return found.ID, found.ID != ""
}

// launch records a new agent and asks for its first session.
func (t *tx) launch(run Run, a Agent) {
	a.Status, a.Stop, a.Session, a.StartedAt = AgentLaunching, StopNone, 1, t.in.At
	t.emit(run.ID, a)
	t.effect(LaunchSession{
		Session: Session{a.ID, 1}, Run: run.ID, Role: a.Role, Spec: a.Spec,
	})
	t.armLaunchDeadline(run.ID, Session{a.ID, 1})
}

// armLaunchDeadline asks for the timer that ends a session still launching after LaunchTimeout.
func (t *tx) armLaunchDeadline(run RunID, session Session) {
	t.effect(ArmTimer{Kind: TimerLaunchDeadline, Run: run, Session: session, FireAt: t.in.At.Add(LaunchTimeout)})
}

func (t *tx) stopRun(b StopRun, r *Receipt) RunID {
	run, ok := t.s.runs[b.Run]
	switch {
	case !ok:
		r.Refusal = RefuseRunNotFound
		return ""
	case run.Status.Final():
		r.Run, r.Refusal = run.ID, RefuseRunNotRunning
		return ""
	}
	r.Run = run.ID
	if run.Status.Live() {
		t.beginStop(run, StopOperator)
	}
	return run.ID
}

func (t *tx) spawnAgent(c Command, b SpawnAgent, r *Receipt) RunID {
	run, ok := t.s.runs[b.Run]
	switch {
	case !ok || !t.callerIn(c, b.Run):
		r.Refusal = RefuseRunNotFound
		return ""
	case run.Status != RunRunning:
		r.Run, r.Refusal = run.ID, RefuseRunNotRunning
		return ""
	case t.liveSubagents(run.ID) >= run.FanoutLimit:
		r.Run, r.Refusal = run.ID, RefuseFanoutLimit
		return ""
	}
	lead := t.s.agents[run.Lead].Agent
	spec := b.Spec
	if spec.Harness == "" {
		spec.Harness = lead.Spec.Harness
	}
	if spec.Harness == lead.Spec.Harness {
		spec.Model = orElse(spec.Model, lead.Spec.Model)
		spec.Effort = orElse(spec.Effort, lead.Spec.Effort)
	}
	id := AgentID(t.mint("agent"))
	t.launch(run, Agent{ID: id, Run: run.ID, Parent: run.Lead, Role: b.Role, Spec: spec, PlanNumber: b.PlanNumber})
	r.Run, r.Agent = run.ID, id
	r.Message = t.post(Message{Run: run.ID, From: callerAgent(c), To: id, Body: Brief{Text: b.Brief}}).ID
	return run.ID
}

// orElse returns the value unless it is empty.
func orElse[T ~string](value, otherwise T) T {
	if value != "" {
		return value
	}
	return otherwise
}

// liveSubagents counts what the fan-out limit limits.
func (t *tx) liveSubagents(run RunID) uint32 {
	var n uint32
	for _, a := range t.s.agents {
		if a.Run == run && a.Role != RoleLead && !a.Status.Over() {
			n++
		}
	}
	return n
}

// target finds the agent a command names, in a run the caller may act on.
func (t *tx) target(c Command, id AgentID, r *Receipt) (agentState, bool) {
	a, ok := t.s.agents[id]
	if !ok || !t.callerIn(c, a.Run) {
		r.Refusal = RefuseAgentNotFound
		return agentState{}, false
	}
	r.Run, r.Agent = a.Run, a.ID
	return a, true
}

func (t *tx) stopSubagent(c Command, b StopAgent, r *Receipt) RunID {
	a, ok := t.target(c, b.Agent, r)
	switch {
	case !ok:
		return ""
	case a.Role == RoleLead:
		r.Refusal = RefuseAgentIsLead
		return ""
	case a.Status.Over() || a.Stop == StopClosing:
		r.Refusal = RefuseAgentNotLive
		return ""
	}
	t.stopAgent(a.ID)
	return a.Run
}

func (t *tx) sendMessage(c Command, b SendMessage, r *Receipt) RunID {
	a, ok := t.target(c, b.Agent, r)
	if !ok {
		return ""
	}
	if !t.s.runs[a.Run].Status.Live() {
		r.Refusal = RefuseRunNotRunning
		return ""
	}
	r.Message = t.post(Message{Run: a.Run, From: callerAgent(c), To: a.ID, Body: Text{Text: b.Text}}).ID
	return a.Run
}

func (t *tx) postHandoff(c Command, b PostHandoff, r *Receipt) RunID {
	caller := t.s.agents[callerAgent(c)]
	caller.HandedOff = true
	t.emit(caller.Run, caller.Agent)
	run := t.s.runs[caller.Run]
	r.Run, r.Agent = run.ID, caller.ID
	r.Message = t.post(Message{Run: run.ID, From: caller.ID, To: run.Lead, Body: b.Handoff}).ID
	return run.ID
}

func (t *tx) postReport(c Command, b PostReport, r *Receipt) RunID {
	caller := t.s.agents[callerAgent(c)]
	r.Run, r.Agent = caller.Run, caller.ID
	run := t.s.runs[caller.Run]
	run.ReportsPosted++
	run.FinalReported = b.Report.Kind == ReportFinal
	t.emit(run.ID, run)
	report := b.Report
	report.Sequence = run.ReportsPosted
	r.Message = t.post(Message{Run: caller.Run, From: caller.ID, Body: report}).ID
	return caller.Run
}

// sortedAgents returns a run's agents in id order, so that no outcome depends on map order.
func (t *tx) sortedAgents(run RunID) []agentState {
	var out []agentState
	for _, id := range slices.Sorted(maps.Keys(t.s.agents)) {
		if a := t.s.agents[id]; a.Run == run {
			out = append(out, a)
		}
	}
	return out
}
