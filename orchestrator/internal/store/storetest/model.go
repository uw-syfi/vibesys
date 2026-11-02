package storetest

import (
	"cmp"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// model answers every read by brute force from the list of everything committed. It shares no code
// with a store: no index, no table, no cache, only loops over the events as a store should return
// them. A store is right when it answers as the model does.
type model struct {
	events []store.EventRecord
	sizes  []int
	// effects are the outbox as it should stand: each effect committed and not completed, with
	// whether it is claimed.
	outbox []modelEffect
}

type modelEffect struct {
	id      core.EffectID
	claimed bool
}

// add records a commit as the store should have made it. It takes the events through the wire, as
// the store does, so the model holds what a reader gets back and not what the writer passed in.
func (m *model) add(r store.Result, c store.Committed) error {
	for i, e := range r.Events {
		seq := c.First + uint64(i)
		b, err := wire.EncodeEvent(seq, e)
		if err != nil {
			return err
		}
		_, back, err := wire.DecodeEvent(b)
		if err != nil {
			return err
		}
		m.events = append(m.events, store.EventRecord{Sequence: seq, Event: back})
		m.sizes = append(m.sizes, len(b))
	}
	for _, e := range r.Effects {
		m.outbox = append(m.outbox, modelEffect{id: e.ID()})
	}
	return nil
}

func (m *model) last() uint64 {
	if len(m.events) == 0 {
		return 0
	}
	return m.events[len(m.events)-1].Sequence
}

func (m *model) state() core.State {
	var all []core.Event
	for _, r := range m.events {
		all = append(all, r.Event)
	}
	return core.Replay(core.State{}, all)
}

func (m *model) snapshot() store.Snapshot {
	runs := map[core.RunID]core.Run{}
	agents := map[core.AgentID]core.Agent{}
	for _, r := range m.events {
		switch b := r.Event.Body.(type) {
		case core.Run:
			runs[b.ID] = b
		case core.Agent:
			agents[b.ID] = b
		}
	}
	out := store.Snapshot{Sequence: m.last()}
	for _, r := range runs {
		out.Runs = append(out.Runs, r)
	}
	for _, a := range agents {
		out.Agents = append(out.Agents, a)
	}
	slices.SortFunc(out.Runs, func(a, b core.Run) int { return cmp.Compare(a.ID, b.ID) })
	slices.SortFunc(out.Agents, func(a, b core.Agent) int { return cmp.Compare(a.ID, b.ID) })
	return out
}

func (m *model) eventsAfter(run core.RunID, after uint64, limit int) []store.EventRecord {
	limit = capped(limit, store.MaxPageEvents)
	var out []store.EventRecord
	for _, r := range m.events {
		if r.Sequence > after && (run == "" || r.Event.Run == run) && len(out) < limit {
			out = append(out, r)
		}
	}
	return out
}

func capped(limit, max int) int {
	if limit <= 0 || limit > max {
		return max
	}
	return limit
}

func (m *model) items(agent core.AgentID, before uint32, limit int) store.ItemPage {
	type held struct {
		item core.Item
		size int
	}
	latest := map[uint32]held{}
	for i, r := range m.events {
		if it, ok := r.Event.Body.(core.Item); ok && it.Agent == agent {
			latest[it.Ordinal] = held{it, m.sizes[i]}
		}
	}
	var ordinals []uint32
	for o := range latest {
		if before == 0 || o < before {
			ordinals = append(ordinals, o)
		}
	}
	slices.SortFunc(ordinals, func(a, b uint32) int { return cmp.Compare(b, a) })
	limit = capped(limit, store.MaxPageItems)
	var page store.ItemPage
	size := 0
	for n, o := range ordinals {
		h := latest[o]
		if n == limit || (n > 0 && size+h.size > store.MaxPageBytes) {
			page.HasOlder = true
			break
		}
		size += h.size
		page.Items = append(page.Items, h.item)
	}
	slices.Reverse(page.Items)
	return page
}

func (m *model) messages(run core.RunID, after uint64, limit int) []store.MessageRecord {
	first := map[core.MessageID]uint64{}
	latest := map[core.MessageID]core.Message{}
	for _, r := range m.events {
		if msg, ok := r.Event.Body.(core.Message); ok && msg.Run == run {
			if _, seen := first[msg.ID]; !seen {
				first[msg.ID] = r.Sequence
			}
			latest[msg.ID] = msg
		}
	}
	var out []store.MessageRecord
	for id, msg := range latest {
		if first[id] > after {
			out = append(out, store.MessageRecord{Seq: first[id], Message: msg})
		}
	}
	slices.SortFunc(out, func(a, b store.MessageRecord) int { return cmp.Compare(a.Seq, b.Seq) })
	return out[:min(len(out), capped(limit, store.MaxPageMessages))]
}

func (m *model) turns(run core.RunID, from, to time.Time, limit int) store.TurnWindow {
	latest := map[core.TurnID]core.Turn{}
	for _, r := range m.events {
		if t, ok := r.Event.Body.(core.Turn); ok && r.Event.Run == run {
			latest[t.ID] = t
		}
	}
	var all []core.Turn
	for _, t := range latest {
		if t.StartedAt.Before(to) && (t.EndedAt.IsZero() || t.EndedAt.After(from)) {
			all = append(all, t)
		}
	}
	slices.SortFunc(all, func(a, b core.Turn) int {
		return cmp.Or(a.StartedAt.Compare(b.StartedAt), cmp.Compare(a.ID, b.ID))
	})
	limit = capped(limit, store.MaxWindowTurns)
	return store.TurnWindow{Turns: all[:min(len(all), limit)], Truncated: len(all) > limit}
}

// runs and agents list what the model knows, for a test to ask about each.
func (m *model) runs() []core.RunID {
	var out []core.RunID
	for _, r := range m.snapshot().Runs {
		out = append(out, r.ID)
	}
	return out
}

func (m *model) agents() []core.AgentID {
	var out []core.AgentID
	for _, a := range m.snapshot().Agents {
		out = append(out, a.ID)
	}
	return out
}
