package main

import (
	"slices"
	"sort"

	"github.com/anishathalye/porcupine"
)

type reservationEventKind uint8

const (
	eventInvalid reservationEventKind = iota
	eventReserve
	eventPublish
	eventFull
	eventDequeue
	eventEmpty
)

type reservationEvent struct {
	Kind          reservationEventKind
	ReservationID int
	Item          priorityItem
}

type reservationState struct {
	Reserved  map[int]priorityItem
	Published []priorityItem
}

func checkReservationAwarePriorityHistory(capacity int, history []recordedOperation) bool {
	operations, ok := reservationAwareOperations(history)
	if !ok {
		return false
	}
	return porcupine.CheckOperations(reservationAwarePriorityModel(capacity), operations)
}

func reservationAwareOperations(history []recordedOperation) ([]porcupine.Operation, bool) {
	operations := make([]porcupine.Operation, 0, len(history)*2)
	for index, op := range history {
		makeOperation := func(kind reservationEventKind, item priorityItem, clientID int) porcupine.Operation {
			return porcupine.Operation{
				ClientId: clientID,
				Input: reservationEvent{
					Kind: kind, ReservationID: index, Item: item,
				},
				Call: op.Call, Output: struct{}{}, Return: op.Return,
			}
		}

		switch op.Input.Kind {
		case "enqueue":
			if op.Input.Value == nil || op.Input.Priority == nil || op.Output.EnqueueOK == nil {
				return nil, false
			}
			item := priorityItem{value: *op.Input.Value, priority: *op.Input.Priority}
			if *op.Output.EnqueueOK {
				operations = append(operations,
					makeOperation(eventReserve, item, index*2),
					makeOperation(eventPublish, item, index*2+1),
				)
			} else {
				operations = append(operations, makeOperation(eventFull, priorityItem{}, index*2))
			}
		case "dequeue":
			switch {
			case op.Output.DequeueNone && op.Output.DequeueVal == nil:
				operations = append(operations, makeOperation(eventEmpty, priorityItem{}, index*2))
			case !op.Output.DequeueNone && op.Output.DequeueVal != nil:
				operations = append(operations, makeOperation(
					eventDequeue, priorityItem{value: *op.Output.DequeueVal}, index*2,
				))
			default:
				return nil, false
			}
		default:
			return nil, false
		}
	}
	return operations, true
}

func reservationAwarePriorityModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any { return reservationState{Reserved: make(map[int]priorityItem)} },
		Step: func(state, input, _ any) (bool, any) {
			current := state.(reservationState)
			event := input.(reservationEvent)
			switch event.Kind {
			case eventReserve:
				if len(current.Reserved)+len(current.Published) >= capacity {
					return false, state
				}
				if _, exists := current.Reserved[event.ReservationID]; exists {
					return false, state
				}
				next := cloneReservationState(current)
				next.Reserved[event.ReservationID] = event.Item
				return true, next
			case eventPublish:
				item, exists := current.Reserved[event.ReservationID]
				if !exists || item != event.Item {
					return false, state
				}
				next := cloneReservationState(current)
				delete(next.Reserved, event.ReservationID)
				next.Published = append(next.Published, item)
				sortPriorityItems(next.Published)
				return true, next
			case eventFull:
				return len(current.Reserved)+len(current.Published) == capacity, state
			case eventDequeue:
				if len(current.Published) == 0 {
					return false, state
				}
				minimum := current.Published[0].priority
				index := slices.IndexFunc(current.Published, func(item priorityItem) bool {
					return item.priority == minimum && item.value == event.Item.value
				})
				if index < 0 {
					return false, state
				}
				next := cloneReservationState(current)
				next.Published = slices.Delete(next.Published, index, index+1)
				return true, next
			case eventEmpty:
				return len(current.Published) == 0, state
			default:
				return false, state
			}
		},
		Equal: func(first, second any) bool {
			a := first.(reservationState)
			b := second.(reservationState)
			if !slices.Equal(a.Published, b.Published) || len(a.Reserved) != len(b.Reserved) {
				return false
			}
			for id, item := range a.Reserved {
				if other, exists := b.Reserved[id]; !exists || other != item {
					return false
				}
			}
			return true
		},
	}
}

func sortPriorityItems(items []priorityItem) {
	sort.Slice(items, func(i, j int) bool {
		if items[i].priority != items[j].priority {
			return items[i].priority < items[j].priority
		}
		return items[i].value < items[j].value
	})
}

func cloneReservationState(state reservationState) reservationState {
	reserved := make(map[int]priorityItem, len(state.Reserved))
	for id, item := range state.Reserved {
		reserved[id] = item
	}
	return reservationState{
		Reserved: reserved, Published: append([]priorityItem(nil), state.Published...),
	}
}
