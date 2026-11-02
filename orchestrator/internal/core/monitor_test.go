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
	retriedDeliveries, operatorStops, deadlineStops, runsFailed, leadsOutOfResumes      int
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
	c.leadsOutOfResumes += o.leadsOutOfResumes
}

func (c coverage) require(t *testing.T) {
	t.Helper()
	t.Logf("coverage: %+v", c)
	for name, n := range map[string]int{
		"handoffs": c.handoffs, "turns on a handoff": c.handoffTurns, "steers": c.steers, "resumes": c.resumes,
		"refused spawns": c.refusedSpawns, "stops": c.stops, "lost agents": c.lost, "rejected messages": c.rejected,
		"finished jobs": c.jobs, "redelivered messages": c.retriedDeliveries, "operator stops": c.operatorStops,
		"deadline stops": c.deadlineStops, "failed runs": c.runsFailed, "leads out of resumes": c.leadsOutOfResumes,
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
	covered  coverage
	turns    map[core.TurnID]core.Turn
	running  map[core.AgentID]core.TurnID
	ordinals map[core.AgentID]uint32
	items    map[core.AgentID]uint32
	open     map[core.TurnID]map[core.ItemID]bool
	agents   map[core.AgentID]core.Agent
	runs     map[core.RunID]core.Run
	messages map[core.MessageID]*messageRecord
	jobs     map[core.JobID]core.MessageID
	receipts map[core.CommandID]core.Receipt
	offsets  map[core.Session]uint64
	// handed holds the agents whose handoff stands: nothing has given them new work since.
	handed map[core.AgentID]bool
	// failures counts each agent's sessions lost in a row, with no completed turn between them.
	failures map[core.AgentID]uint32

	// The rest lasts one transaction.
	refusedSteer core.MessageID
	idled        map[core.AgentID]bool
	over         map[core.AgentID]bool
	noticed      map[core.AgentID]int
	resumed      map[core.AgentID]bool
}

func newMonitor() *monitor {
	return &monitor{
		turns: map[core.TurnID]core.Turn{}, running: map[core.AgentID]core.TurnID{}, ordinals: map[core.AgentID]uint32{},
		items: map[core.AgentID]uint32{}, open: map[core.TurnID]map[core.ItemID]bool{}, agents: map[core.AgentID]core.Agent{},
		runs: map[core.RunID]core.Run{}, messages: map[core.MessageID]*messageRecord{}, jobs: map[core.JobID]core.MessageID{},
		receipts: map[core.CommandID]core.Receipt{}, offsets: map[core.Session]uint64{}, handed: map[core.AgentID]bool{},
		failures: map[core.AgentID]uint32{},
	}
}

// commit checks one transaction, in order: what an input may change, then each event, then what
// must have come of them taken together.
func (m *monitor) commit(body core.InputBody, events []core.Event) error {
	m.refusedSteer = ""
	m.idled, m.over, m.noticed, m.resumed = map[core.AgentID]bool{}, map[core.AgentID]bool{}, map[core.AgentID]int{}, map[core.AgentID]bool{}
	if fact, ok := body.(core.SidecarFact); ok {
		if outcome, ok := fact.Body.(core.SteerOutcome); ok && !outcome.Accepted {
			m.refusedSteer = outcome.Message
		}
		if err := m.fact(fact, events); err != nil {
			return err
		}
	}
	for _, e := range events {
		if err := m.observe(e); err != nil {
			return err
		}
	}
	return m.settled()
}

// fact holds a sidecar to its session: only the current one has a say, and each offset counts once.
func (m *monitor) fact(f core.SidecarFact, events []core.Event) error {
	if len(events) == 0 {
		return nil
	}
	a := m.agents[f.Session.Agent]
	if a.Session != f.Session.N || a.Status.Over() {
		return fmt.Errorf("a fact of session %v changed state, and the agent is at session %d, %s", f.Session, a.Session, a.Status)
	}
	if f.Offset <= m.offsets[f.Session] {
		return fmt.Errorf("offset %d of session %v applied after offset %d", f.Offset, f.Session, m.offsets[f.Session])
	}
	m.offsets[f.Session] = f.Offset
	return nil
}

// settled holds what no single event shows: no subagent stops working unheard, and no message stays
// steered at a session that is gone.
func (m *monitor) settled() error {
	for agent := range m.idled {
		a := m.agents[agent]
		if a.Status.Resting() && a.Stop == core.StopNone && !m.owedMail(agent) {
			m.over[agent] = true
		}
	}
	for agent := range m.over {
		a := m.agents[agent]
		if a.Role == core.RoleLead || m.handed[agent] || m.runs[a.Run].Status != core.RunRunning || a.Status == core.AgentLaunching {
			continue
		}
		if m.noticed[agent] != 1 {
			return fmt.Errorf("agent %s stopped working (%s) with no handoff, and the lead heard %d times", agent, a.Status, m.noticed[agent])
		}
	}
	for agent := range m.resumed {
		for id, record := range m.messages {
			if record.to == agent && record.last == core.DeliverySteered {
				return fmt.Errorf("message %s stayed steered at a session of %s that is gone", id, agent)
			}
		}
	}
	return nil
}

func (m *monitor) owedMail(agent core.AgentID) bool {
	for _, record := range m.messages {
		if record.to == agent && (record.last == core.DeliveryQueued || record.last == core.DeliverySteered) {
			return true
		}
	}
	return false
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
	case r.Status == core.RunStopping && r.StopReason == core.StopLeadGone:
		lead := m.agents[r.Lead]
		if lead.Capabilities.Resumes && lead.ConversationID != "" && m.failures[lead.ID] <= core.MaxLeadResumes {
			return fmt.Errorf("run %s gave up on a lead with %d failures that could resume", r.ID, m.failures[lead.ID])
		}
		if lead.Capabilities.Resumes {
			m.covered.leadsOutOfResumes++
		}
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
		if a.Role == core.RoleLead && m.failures[a.ID] > core.MaxLeadResumes {
			return fmt.Errorf("lead %s resumed after %d failures in a row", a.ID, m.failures[a.ID])
		}
		delete(m.handed, a.ID)
		m.resumed[a.ID] = true
	}
	if a.Status.Over() && !old.Status.Over() {
		m.over[a.ID] = true
		if old.Stop == core.StopNone {
			m.failures[a.ID]++
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
		delete(m.handed, t.Agent)
		delete(m.idled, t.Agent)
	case old.Status != core.TurnRunning:
		return fmt.Errorf("turn %s changed after it ended: %+v", t.ID, t)
	case t.Status != core.TurnRunning:
		delete(m.running, t.Agent)
		record.running--
		if t.Status == core.TurnCompleted {
			record.finished++
			m.failures[t.Agent] = 0
		}
		if len(m.open[t.ID]) > 0 {
			return fmt.Errorf("turn %s ended with items open: %v", t.ID, m.open[t.ID])
		}
		delete(m.open, t.ID)
		m.idled[t.Agent] = true
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
	if m.open[i.Turn] == nil {
		m.open[i.Turn] = map[core.ItemID]bool{}
	}
	if i.Status == core.ItemRunning {
		m.open[i.Turn][i.ID] = true
	} else {
		delete(m.open[i.Turn], i.ID)
	}
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
			m.handed[msg.From] = true
		case core.SubagentSettled:
			m.noticed[b.Agent]++
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
		if a := m.agents[msg.To]; a.Status != core.AgentActive || a.Stop != core.StopNone || !a.Capabilities.Steers {
			return fmt.Errorf("message %s steered at %+v", msg.ID, a)
		}
	}
	if msg.Delivery == core.DeliveryConsumed && msg.ID == m.refusedSteer {
		return fmt.Errorf("message %s consumed by a steer its sidecar refused", msg.ID)
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
