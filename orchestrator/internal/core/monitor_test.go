package core_test

import (
	"fmt"
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

func timestamp(t time.Time) *timestamppb.Timestamp { return timestamppb.New(t) }

// coverage counts what a simulation reached, so that a property cannot pass by never being tested.
type coverage struct {
	handoffs, handoffTurns, steers, resumes, refusedSpawns, stops, lost, rejected, jobs int
	retriedDeliveries, operatorStops, deadlineStops, runsFailed                         int
}

func (c *coverage) add(o coverage) {
	c.handoffs += o.handoffs
	c.handoffTurns += o.handoffTurns
	c.steers += o.steers
	c.resumes += o.resumes
	c.refusedSpawns += o.refusedSpawns
	c.stops += o.stops
	c.lost += o.lost
	c.rejected += o.rejected
	c.jobs += o.jobs
	c.retriedDeliveries += o.retriedDeliveries
	c.operatorStops += o.operatorStops
	c.deadlineStops += o.deadlineStops
	c.runsFailed += o.runsFailed
}

func (c coverage) require(t *testing.T) {
	t.Helper()
	t.Logf("coverage: %+v", c)
	for name, n := range map[string]int{
		"handoffs": c.handoffs, "turns on a handoff": c.handoffTurns, "steers": c.steers, "resumes": c.resumes,
		"refused spawns": c.refusedSpawns, "stops": c.stops, "lost agents": c.lost, "rejected messages": c.rejected,
		"finished jobs": c.jobs, "redelivered messages": c.retriedDeliveries, "operator stops": c.operatorStops,
		"deadline stops": c.deadlineStops, "failed runs": c.runsFailed,
	} {
		if n == 0 {
			t.Errorf("the simulation never reached: %s", name)
		}
	}
}

// messageRecord is the monitor's own account of one message.
type messageRecord struct {
	to       core.AgentID
	body     core.MessageBody
	last     core.Delivery
	previous core.Delivery
	settled  int
	turns    int
	running  int
	finished int
}

// monitor checks the guarantees from the committed log alone, keeping its own books, so it shares
// no code and no bug with the core.
type monitor struct {
	covered   coverage
	turns     map[core.TurnID]core.Turn
	running   map[core.AgentID]core.TurnID
	ordinals  map[core.AgentID]uint32
	items     map[core.AgentID]uint32
	agents    map[core.AgentID]core.Agent
	runs      map[core.RunID]core.Run
	messages  map[core.MessageID]*messageRecord
	jobs      map[core.JobID]core.MessageID
	receipts  map[core.CommandID]core.Receipt
	lastAgent map[core.AgentID]uint32
}

func newMonitor() *monitor {
	return &monitor{
		turns: map[core.TurnID]core.Turn{}, running: map[core.AgentID]core.TurnID{}, ordinals: map[core.AgentID]uint32{},
		items: map[core.AgentID]uint32{}, agents: map[core.AgentID]core.Agent{}, runs: map[core.RunID]core.Run{},
		messages: map[core.MessageID]*messageRecord{}, jobs: map[core.JobID]core.MessageID{},
		receipts: map[core.CommandID]core.Receipt{}, lastAgent: map[core.AgentID]uint32{},
	}
}

func (m *monitor) observe(e core.Event) error {
	switch b := e.Body.(type) {
	case core.Run:
		return m.run(b)
	case core.Agent:
		return m.agent(b)
	case core.Turn:
		return m.turn(b)
	case core.Item:
		return m.item(b)
	case core.Message:
		return m.message(b)
	case core.Receipt:
		return m.receipt(b)
	}
	return fmt.Errorf("unknown event %T", e.Body)
}

func (m *monitor) run(r core.Run) error {
	if old, ok := m.runs[r.ID]; ok && old.Status.Final() {
		return fmt.Errorf("run %s changed after it finished: %+v", r.ID, r)
	}
	switch {
	case r.Status == core.RunFailed && r.StopReason != core.StopLeadGone:
		return fmt.Errorf("run %s failed for %q", r.ID, r.StopReason)
	case r.Status == core.RunFailed:
		m.covered.runsFailed++
	case r.Status == core.RunEnded && r.StopReason == core.StopDeadline:
		m.covered.deadlineStops++
	case r.Status == core.RunEnded:
		m.covered.operatorStops++
	}
	m.runs[r.ID] = r
	return nil
}

func (m *monitor) agent(a core.Agent) error {
	old, known := m.agents[a.ID]
	m.agents[a.ID] = a
	if known && a.Session != old.Session {
		m.covered.resumes++
		if a.Session != old.Session+1 || !old.Status.Over() {
			return fmt.Errorf("agent %s went from %+v to session %d", a.ID, old, a.Session)
		}
	}
	if a.Status == core.AgentLost {
		m.covered.lost++
	}
	if a.Stop != core.StopNone && old.Stop == core.StopNone {
		m.covered.stops++
	}
	if a.Role == core.RoleLead {
		return nil
	}
	var live uint32
	for _, other := range m.agents {
		if other.Run == a.Run && other.Role != core.RoleLead && !other.Status.Over() {
			live++
		}
	}
	if limit := m.runs[a.Run].FanoutLimit; live > limit {
		return fmt.Errorf("run %s has %d live subagents over its limit of %d", a.Run, live, limit)
	}
	return nil
}

func (m *monitor) turn(t core.Turn) error {
	running, busy := m.running[t.Agent]
	record := m.messages[t.Message]
	if record == nil {
		return fmt.Errorf("turn %s is for unknown message %s", t.ID, t.Message)
	}
	old, known := m.turns[t.ID]
	m.turns[t.ID] = t
	switch {
	case !known:
		if busy {
			return fmt.Errorf("agent %s started turn %s while turn %s runs", t.Agent, t.ID, running)
		}
		if t.Status != core.TurnRunning || t.Ordinal != m.ordinals[t.Agent]+1 {
			return fmt.Errorf("turn %s starts as %+v after ordinal %d", t.ID, t, m.ordinals[t.Agent])
		}
		m.ordinals[t.Agent] = t.Ordinal
		m.running[t.Agent] = t.ID
		record.turns++
		record.running++
		if record.running > 1 {
			return fmt.Errorf("message %s started two running turns", t.Message)
		}
		if record.finished > 0 {
			return fmt.Errorf("message %s started a turn after one completed it", t.Message)
		}
		if _, ok := record.body.(core.Handoff); ok {
			m.covered.handoffTurns++
		}
		if record.turns > 1 {
			m.covered.retriedDeliveries++
		}
	case old.Status != core.TurnRunning:
		return fmt.Errorf("turn %s changed after it ended: %+v", t.ID, t)
	case t.Status != core.TurnRunning:
		delete(m.running, t.Agent)
		record.running--
		if t.Status == core.TurnCompleted {
			record.finished++
		}
	}
	return nil
}

func (m *monitor) item(i core.Item) error {
	if m.running[i.Agent] != i.Turn {
		return fmt.Errorf("item %s of turn %s, which is not running", i.ID, i.Turn)
	}
	if i.Ordinal > m.items[i.Agent]+1 {
		return fmt.Errorf("item %s skips to ordinal %d from %d", i.ID, i.Ordinal, m.items[i.Agent])
	}
	m.items[i.Agent] = max(m.items[i.Agent], i.Ordinal)
	return nil
}

func (m *monitor) message(msg core.Message) error {
	record := m.messages[msg.ID]
	if record == nil {
		record = &messageRecord{to: msg.To, body: msg.Body}
		m.messages[msg.ID] = record
		switch b := msg.Body.(type) {
		case core.Handoff:
			m.covered.handoffs++
		case core.JobFinished:
			m.covered.jobs++
			if first, told := m.jobs[b.Job]; told {
				return fmt.Errorf("job %s told twice: %s and %s", b.Job, first, msg.ID)
			}
			m.jobs[b.Job] = msg.ID
		}
	}
	if record.last.Final() {
		return fmt.Errorf("message %s changed after it settled: %+v", msg.ID, msg)
	}
	if msg.Delivery == core.DeliverySteered && record.last != core.DeliverySteered {
		m.covered.steers++
	}
	if msg.Delivery.Final() {
		record.settled++
	}
	if msg.Delivery == core.DeliveryRejected {
		m.covered.rejected++
		if msg.Rejection == "" {
			return fmt.Errorf("message %s rejected with no reason", msg.ID)
		}
	}
	if msg.Delivery == core.DeliveryNewTurn && msg.Attempts > core.MaxDeliveryAttempts {
		return fmt.Errorf("message %s took %d attempts", msg.ID, msg.Attempts)
	}
	record.previous, record.last = record.last, msg.Delivery
	return nil
}

func (m *monitor) receipt(r core.Receipt) error {
	if old, ok := m.receipts[r.Command]; ok {
		return fmt.Errorf("command %s got a second receipt: %+v, then %+v", r.Command, old, r)
	}
	m.receipts[r.Command] = r
	if r.Refusal == core.RefuseFanoutLimit {
		m.covered.refusedSpawns++
	}
	return nil
}

// final holds what must be true when everything has settled. Every message reached the end once,
// and one consumed by an agent was taken exactly once: by the turn that completed it, or by the
// running turn it steered, never both. That is what makes a wake happen exactly once.
func (m *monitor) final() error {
	for id, record := range m.messages {
		if record.settled != 1 {
			return fmt.Errorf("message %s settled %d times, last as %s", id, record.settled, record.last)
		}
		wanted := 0
		if record.last == core.DeliveryConsumed && record.to != "" && record.previous != core.DeliverySteered {
			wanted = 1
		}
		if record.finished != wanted {
			return fmt.Errorf("message %s completed %d turns, want %d: it went from %s to %s",
				id, record.finished, wanted, record.previous, record.last)
		}
		if record.running != 0 {
			return fmt.Errorf("message %s ended as %s with a turn running", id, record.last)
		}
	}
	for agent, turn := range m.running {
		return fmt.Errorf("agent %s still runs turn %s", agent, turn)
	}
	return nil
}
