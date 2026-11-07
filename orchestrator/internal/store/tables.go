package store

import (
	"fmt"
	"reflect"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// The tables are projections of the event log: each holds, for one kind of entity, the body of the
// latest event that wrote it. A reader decodes that body with wire.DecodeEvent, so the bytes a
// table holds are the bytes the log holds, and a table can be rebuilt from the log.

// row is one entity in a table.
type row struct {
	// first is the sequence of the first event that wrote the entity. Only messages keep it, for
	// their order.
	first uint64
	// last is the sequence of the latest.
	last uint64
	// body is the latest event as wire.EncodeEvent made it.
	body []byte
	// event is body decoded, which only the Memory store keeps.
	event core.Event
}

// tables is the projection of a log, held in memory. Memory serves its reads from it, and SQLite's
// Verify compares it with the file's tables.
type tables struct {
	runs     map[core.RunID]row
	agents   map[core.AgentID]row
	turns    map[core.TurnID]row
	items    map[core.ItemID]row
	messages map[core.MessageID]row
	receipts map[core.CommandID]row
	checkIns map[core.CheckInID]row
}

func newTables() *tables {
	return &tables{
		runs: map[core.RunID]row{}, agents: map[core.AgentID]row{}, turns: map[core.TurnID]row{},
		items: map[core.ItemID]row{}, messages: map[core.MessageID]row{}, receipts: map[core.CommandID]row{},
		checkIns: map[core.CheckInID]row{},
	}
}

// apply folds one committed event into the tables.
func (t *tables) apply(seq uint64, e core.Event, body []byte) error {
	r := row{last: seq, body: body, event: e}
	switch b := e.Body.(type) {
	case core.Run:
		t.runs[b.ID] = r
	case core.Agent:
		t.agents[b.ID] = r
	case core.Turn:
		t.turns[b.ID] = r
	case core.Item:
		t.items[b.ID] = r
	case core.Message:
		r.first = seq
		if old, ok := t.messages[b.ID]; ok {
			r.first = old.first
		}
		t.messages[b.ID] = r
	case core.Receipt:
		t.receipts[b.Command] = r
	case core.CheckIn:
		t.checkIns[b.ID] = r
	default:
		return fmt.Errorf("store: unknown event body %T", e.Body)
	}
	return nil
}

// clash finds what the unique keys of the tables would refuse: two items of one agent with one
// ordinal, or two turns. It reads the tables and the events about to be applied, so a commit can
// be refused before any of it is written. SQLite refuses the same through its unique indexes.
type clash struct {
	items map[agentOrdinal]core.ItemID
	turns map[agentOrdinal]core.TurnID
}

type agentOrdinal struct {
	agent   core.AgentID
	ordinal uint32
}

func newClash(t *tables) *clash {
	c := &clash{items: map[agentOrdinal]core.ItemID{}, turns: map[agentOrdinal]core.TurnID{}}
	for _, r := range t.items {
		i := r.event.Body.(core.Item)
		c.items[agentOrdinal{i.Agent, i.Ordinal}] = i.ID
	}
	for _, r := range t.turns {
		u := r.event.Body.(core.Turn)
		c.turns[agentOrdinal{u.Agent, u.Ordinal}] = u.ID
	}
	return c
}

func (c *clash) check(e core.Event) error {
	switch b := e.Body.(type) {
	case core.Item:
		k := agentOrdinal{b.Agent, b.Ordinal}
		if id, ok := c.items[k]; ok && id != b.ID {
			return fmt.Errorf("store: item %s and %s of agent %s share ordinal %d", id, b.ID, b.Agent, b.Ordinal)
		}
		c.items[k] = b.ID
	case core.Turn:
		k := agentOrdinal{b.Agent, b.Ordinal}
		if id, ok := c.turns[k]; ok && id != b.ID {
			return fmt.Errorf("store: turn %s and %s of agent %s share ordinal %d", id, b.ID, b.Agent, b.Ordinal)
		}
		c.turns[k] = b.ID
	}
	return nil
}

// encodeResult gives a result's events and effects their stored form. Sequences count up from
// first, and an effect is refused unless an event came with it.
func encodeResult(first uint64, r Result) (events [][]byte, effects [][]byte, err error) {
	if len(r.Events) == 0 && len(r.Effects) > 0 {
		return nil, nil, fmt.Errorf("store: %d effects with no event to cause them", len(r.Effects))
	}
	for i, e := range r.Events {
		b, err := wire.EncodeEvent(first+uint64(i), e)
		if err != nil {
			return nil, nil, fmt.Errorf("store: encode event %d of %d: %w", i+1, len(r.Events), err)
		}
		events = append(events, b)
	}
	for i, e := range r.Effects {
		b, err := wire.EncodeEffect(e)
		if err != nil {
			return nil, nil, fmt.Errorf("store: encode effect %d of %d: %w", i+1, len(r.Effects), err)
		}
		effects = append(effects, b)
	}
	return events, effects, nil
}

// runAfter is when an effect becomes ready, and false for an effect that is ready at once.
func runAfter(e core.Effect) (nanos int64, timed bool) {
	if t, ok := e.(core.ArmTimer); ok {
		return t.FireAt.UnixNano(), true
	}
	return 0, false
}

// summary is the part of the tables that must equal the state: what a state holds for runs, agents,
// mail in transit, receipts and check-ins. Items, turns and settled messages are history, which no
// state keeps.
type summary struct {
	runs     map[core.RunID]core.Run
	agents   map[core.AgentID]core.Agent
	transit  map[core.MessageID]core.Message
	receipts map[core.CommandID]core.Receipt
	checkIns map[core.CheckInID]core.CheckIn
}

func newSummary() summary {
	return summary{
		runs: map[core.RunID]core.Run{}, agents: map[core.AgentID]core.Agent{},
		transit: map[core.MessageID]core.Message{}, receipts: map[core.CommandID]core.Receipt{},
		checkIns: map[core.CheckInID]core.CheckIn{},
	}
}

// summary reads a summary out of memory tables.
func (t *tables) summary() summary {
	out := newSummary()
	for id, r := range t.runs {
		out.runs[id] = r.event.Body.(core.Run)
	}
	for id, r := range t.agents {
		out.agents[id] = r.event.Body.(core.Agent)
	}
	for id, r := range t.messages {
		if m := r.event.Body.(core.Message); !m.Delivery.Final() {
			out.transit[id] = m
		}
	}
	for id, r := range t.receipts {
		out.receipts[id] = r.event.Body.(core.Receipt)
	}
	for id, r := range t.checkIns {
		out.checkIns[id] = r.event.Body.(core.CheckIn)
	}
	return out
}

// check compares a summary with a state and names the first difference.
func (sm summary) check(s core.State) error {
	want := newSummary()
	for _, run := range s.Runs() {
		want.runs[run.ID] = run
		for _, a := range s.Agents(run.ID) {
			want.agents[a.ID] = a
		}
		for _, c := range s.CheckIns(run.ID) {
			want.checkIns[c.ID] = c
		}
	}
	for _, m := range s.InTransit() {
		want.transit[m.ID] = m
	}
	for _, e := range s.Checkpoint() {
		if r, ok := e.Body.(core.Receipt); ok {
			want.receipts[r.Command] = r
		}
	}
	if err := sameSet("runs", want.runs, sm.runs); err != nil {
		return err
	}
	if err := sameSet("agents", want.agents, sm.agents); err != nil {
		return err
	}
	if err := sameSet("messages in transit", want.transit, sm.transit); err != nil {
		return err
	}
	if err := sameSet("receipts", want.receipts, sm.receipts); err != nil {
		return err
	}
	return sameSet("check-ins", want.checkIns, sm.checkIns)
}

// sameSet compares what a replay gives (want) with what a table holds (got).
func sameSet[K comparable, V any](what string, want, got map[K]V) error {
	for k, w := range want {
		g, ok := got[k]
		switch {
		case !ok:
			return corruptf("%s: a replay has %v and the table does not", what, k)
		case !reflect.DeepEqual(w, g):
			return corruptf("%s: %v differs.\n replay: %+v\n table:  %+v", what, k, w, g)
		}
	}
	for k := range got {
		if _, ok := want[k]; !ok {
			return corruptf("%s: the table has %v and a replay does not", what, k)
		}
	}
	return nil
}
