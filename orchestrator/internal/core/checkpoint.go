package core

import (
	"cmp"
	"maps"
	"reflect"
	"slices"
	"time"
)

// checkpointAt is the time on the events a checkpoint invents. They are never shown to anyone.
var checkpointAt = time.Unix(0, 0).UTC()

// checkpointID names the entities a checkpoint invents, which no real id can: real ids carry a
// nonce and a counter.
const checkpointID = "checkpoint"

// Checkpoint returns events that Replay folds, from the zero State, into a State equal to s. It is
// how a store holds a state in the shape it already holds history, so that a restart loads a
// checkpoint and replays only the events after it instead of all of them.
//
// The events are a description of the state and not a history: a few of them are invented to
// carry what the fold keeps without an entity, namely the counters of turns and items an agent has
// used, and the jobs already told, each under the run it was told to. They pass the contract like any event, and no reader sees them.
// They come in an order that makes the fold rebuild the queues, so it is part of what Checkpoint
// returns.
func (s State) Checkpoint() []Event {
	var out []Event
	emit := func(run RunID, body EventBody) {
		out = append(out, Event{Run: run, At: checkpointAt, Body: body})
	}
	for _, id := range slices.Sorted(maps.Keys(s.runs)) {
		emit(id, s.runs[id])
	}
	for _, id := range slices.Sorted(maps.Keys(s.agents)) {
		a := s.agents[id]
		agent := Event{Run: a.Run, At: checkpointAt, Body: a.Agent}
		if a.offset > 0 {
			agent.Source = &SessionSource{Session: Session{Agent: a.ID, N: a.Session}, Offset: a.offset}
		}
		out = append(out, agent)
		// The counters first, then the true running turn, so the fold ends with that turn running
		// and its items in place.
		if a.items > 0 {
			emit(a.Run, Item{
				ID: checkpointID, Turn: checkpointID, Agent: a.ID, Ordinal: a.items, Status: ItemFinished,
				At: checkpointAt, Body: Assistant{},
			})
		}
		// A completed turn clears what the invented item left in current, and sets the counter.
		if a.turns > 0 && (a.turn == nil || a.turn.Ordinal < a.turns || a.items > 0) {
			emit(a.Run, Turn{
				ID: checkpointID, Agent: a.ID, Ordinal: a.turns, Trigger: TriggerMessage, Message: checkpointID,
				Status: TurnCompleted, StartedAt: checkpointAt,
			})
		}
		if a.turn != nil {
			emit(a.Run, *a.turn)
		}
		for _, itemID := range slices.Sorted(maps.Keys(a.current)) {
			i := a.current[itemID]
			if i.Body == nil {
				i.Body = Assistant{}
			}
			emit(a.Run, i)
		}
	}
	// A queue is its messages in order, and the fold appends each queued message as it sees it.
	// Messages are not in a queue and still in transit come after, in id order.
	queued := map[MessageID]bool{}
	for _, id := range slices.Sorted(maps.Keys(s.agents)) {
		for _, m := range s.agents[id].queue {
			queued[m] = true
			emit(s.messages[m].Run, s.messages[m])
		}
	}
	for _, id := range slices.Sorted(maps.Keys(s.messages)) {
		if !queued[id] {
			emit(s.messages[id].Run, s.messages[id])
		}
	}
	for _, id := range slices.Sorted(maps.Keys(s.receipts)) {
		r := s.receipts[id]
		out = append(out, Event{Run: r.Run, Command: r.Command, At: checkpointAt, Body: r})
	}
	for _, id := range slices.Sorted(maps.Keys(s.checkIns)) {
		emit(s.checkIns[id].Run, s.checkIns[id])
	}
	for _, told := range slices.SortedFunc(maps.Keys(s.jobs), func(a, b toldJob) int {
		return cmp.Or(cmp.Compare(a.run, b.run), cmp.Compare(a.job, b.job))
	}) {
		emit(told.run, Message{
			ID:  MessageID(checkpointID + "/" + string(told.run) + "/" + string(told.job)),
			Run: told.run, Delivery: DeliveryConsumed, At: checkpointAt,
			Body: JobFinished{Job: told.job, Outcome: JobSucceeded},
		})
	}
	return out
}

// Equal says two states are the same memory: the same runs, agents, mail and receipts, the same
// queues and the same counters, however each came to be. Times compare as values, so it is meant
// for states built from stored events.
func (s State) Equal(o State) bool {
	return reflect.DeepEqual(s.canonical(), o.canonical())
}

// canonical gives the state one form for every history: a map or queue that was never written and
// one written and emptied are the same, and a running turn is its own copy.
func (s State) canonical() State {
	out := State{
		runs: nilIfEmpty(s.runs), messages: nilIfEmpty(s.messages), receipts: nilIfEmpty(s.receipts), checkIns: nilIfEmpty(s.checkIns),
		jobs: nilIfEmpty(s.jobs), agents: nilIfEmpty(maps.Clone(s.agents)),
	}
	for id, a := range out.agents {
		a.current, a.queue = nilIfEmpty(a.current), slices.Clip(a.queue)
		if len(a.queue) == 0 {
			a.queue = nil
		}
		out.agents[id] = a
	}
	return out
}

func nilIfEmpty[K comparable, V any](m map[K]V) map[K]V {
	if len(m) == 0 {
		return nil
	}
	return m
}
