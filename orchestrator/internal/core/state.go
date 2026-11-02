package core

import (
	"cmp"
	"fmt"
	"maps"
	"slices"
)

// agentState is what the core keeps for an agent. All of it but the Agent is derived by the fold.
type agentState struct {
	Agent
	// turn is the running turn, if any.
	turn *Turn
	// turns and items are the highest ordinals handed out.
	turns, items uint32
	// current holds the running turn's items. A finished item keeps its place and loses its body,
	// which the store has.
	current map[ItemID]Item
	// queue holds the ids of the messages waiting for a turn, oldest first.
	queue []MessageID
	// offset is the last committed sidecar offset of the current session.
	offset uint64
}

// State is the core's whole memory. It is a value: Decide returns a new State and leaves the old
// one as it was, and the zero State is the state of a daemon with no history.
type State struct {
	runs   map[RunID]Run
	agents map[AgentID]agentState
	// messages holds the messages still in transit.
	messages map[MessageID]Message
	receipts map[CommandID]Receipt
	// jobs holds the jobs whose end was already told.
	jobs map[JobID]struct{}
}

// clone returns a State whose top-level maps can be written. What they hold is replaced and never
// changed in place, so it is shared.
func (s State) clone() State {
	out := State{
		runs:     maps.Clone(s.runs),
		agents:   maps.Clone(s.agents),
		messages: maps.Clone(s.messages),
		receipts: maps.Clone(s.receipts),
		jobs:     maps.Clone(s.jobs),
	}
	if out.runs == nil {
		out.runs = map[RunID]Run{}
		out.agents = map[AgentID]agentState{}
		out.messages = map[MessageID]Message{}
		out.receipts = map[CommandID]Receipt{}
		out.jobs = map[JobID]struct{}{}
	}
	return out
}

// Replay folds committed events into s. A restart is a Replay from the zero State.
func Replay(s State, events []Event) State {
	s = s.clone()
	for _, e := range events {
		s.apply(e)
	}
	return s
}

// apply is the one fold: every change to a State is an Event passing through here. It writes to s,
// so s must be a clone.
func (s *State) apply(e Event) {
	switch b := e.Body.(type) {
	case Run:
		s.runs[b.ID] = b
	case Agent:
		a := s.agents[b.ID]
		if a.Session != b.Session {
			a.offset = 0
		}
		a.Agent = b
		s.agents[b.ID] = a
	case Turn:
		a := s.agents[b.Agent]
		a.turns = max(a.turns, b.Ordinal)
		if b.Status == TurnRunning {
			a.turn = &b
		} else {
			a.turn, a.current = nil, nil
		}
		s.agents[b.Agent] = a
	case Item:
		a := s.agents[b.Agent]
		a.items = max(a.items, b.Ordinal)
		a.current = maps.Clone(a.current)
		if a.current == nil {
			a.current = map[ItemID]Item{}
		}
		if b.Status != ItemRunning {
			b.Body = nil
		}
		a.current[b.ID] = b
		s.agents[b.Agent] = a
	case Message:
		s.applyMessage(b)
	case Receipt:
		s.receipts[b.Command] = b
	default:
		panic(fmt.Sprintf("core: unknown event body %T", e.Body))
	}
	if src := e.Source; src != nil {
		if a, ok := s.agents[src.Session.Agent]; ok && a.Session == src.Session.N {
			a.offset = max(a.offset, src.Offset)
			s.agents[src.Session.Agent] = a
		}
	}
}

func (s *State) applyMessage(m Message) {
	if a, ok := s.agents[m.To]; ok {
		queue := slices.DeleteFunc(slices.Clone(a.queue), func(id MessageID) bool { return id == m.ID })
		if m.Delivery == DeliveryQueued {
			queue = append(queue, m.ID)
		}
		if len(queue) == 0 {
			queue = nil
		}
		a.queue = queue
		s.agents[m.To] = a
	}
	if m.Delivery.Final() {
		delete(s.messages, m.ID)
	} else {
		s.messages[m.ID] = m
	}
	if job, ok := m.Body.(JobFinished); ok {
		s.jobs[job.Job] = struct{}{}
	}
}

// Run returns a run.
func (s State) Run(id RunID) (Run, bool) {
	r, ok := s.runs[id]
	return r, ok
}

// Runs returns every run, in id order.
func (s State) Runs() []Run {
	out := make([]Run, 0, len(s.runs))
	for _, id := range slices.Sorted(maps.Keys(s.runs)) {
		out = append(out, s.runs[id])
	}
	return out
}

// Agent returns an agent.
func (s State) Agent(id AgentID) (Agent, bool) {
	a, ok := s.agents[id]
	return a.Agent, ok
}

// Agents returns a run's agents, in id order.
func (s State) Agents(run RunID) []Agent {
	var out []Agent
	for _, a := range s.agents {
		if a.Run == run {
			out = append(out, a.Agent)
		}
	}
	slices.SortFunc(out, func(x, y Agent) int { return cmp.Compare(x.ID, y.ID) })
	return out
}

// ActiveTurn returns the turn an agent is running.
func (s State) ActiveTurn(id AgentID) (Turn, bool) {
	if t := s.agents[id].turn; t != nil {
		return *t, true
	}
	return Turn{}, false
}

// InTransit returns the messages still being delivered, in id order.
func (s State) InTransit() []Message {
	out := make([]Message, 0, len(s.messages))
	for _, id := range slices.Sorted(maps.Keys(s.messages)) {
		out = append(out, s.messages[id])
	}
	return out
}

// CommittedOffset is the sidecar offset a session's stream resumes after.
func (s State) CommittedOffset(session Session) uint64 {
	if a, ok := s.agents[session.Agent]; ok && a.Session == session.N {
		return a.offset
	}
	return 0
}

// Recall is what a state knows of a command id.
type Recall string

// The recalls.
const (
	// RecallNew means the command has not been seen.
	RecallNew Recall = "new"
	// RecallRepeat means the same command was seen, and the receipt is its answer.
	RecallRepeat Recall = "repeat"
	// RecallConflict means the id was seen on another command, and the shell must refuse this one.
	RecallConflict Recall = "conflict"
)

// Recall looks up a command by id and digest.
func (s State) Recall(id CommandID, digest Digest) (Receipt, Recall) {
	r, ok := s.receipts[id]
	switch {
	case !ok:
		return Receipt{}, RecallNew
	case r.Digest != digest:
		return r, RecallConflict
	}
	return r, RecallRepeat
}
