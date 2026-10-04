package main

import (
	"slices"

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
	Item          stackItem
}

type reservationState struct {
	Reserved  map[int]stackItem
	Published []stackItem
}

func checkReservationAwareStackHistory(capacity int, history []recordedOperation) bool {
	operations, ok := reservationAwareOperations(history)
	if !ok {
		return false
	}
	return porcupine.CheckOperations(reservationAwareStackModel(capacity), operations)
}

func reservationAwareOperations(history []recordedOperation) ([]porcupine.Operation, bool) {
	operations := make([]porcupine.Operation, 0, len(history)*2)
	for index, op := range history {
		makeOperation := func(kind reservationEventKind, item stackItem, clientID int) porcupine.Operation {
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
			if op.Input.Value == nil || op.Output.EnqueueOK == nil {
				return nil, false
			}
			item := stackItem{value: *op.Input.Value}
			if *op.Output.EnqueueOK {
				operations = append(operations,
					makeOperation(eventReserve, item, index*2),
					makeOperation(eventPublish, item, index*2+1),
				)
			} else {
				operations = append(operations, makeOperation(eventFull, stackItem{}, index*2))
			}
		case "dequeue":
			switch {
			case op.Output.DequeueNone && op.Output.DequeueVal == nil:
				operations = append(operations, makeOperation(eventEmpty, stackItem{}, index*2))
			case !op.Output.DequeueNone && op.Output.DequeueVal != nil:
				operations = append(operations, makeOperation(
					eventDequeue, stackItem{value: *op.Output.DequeueVal}, index*2,
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

func reservationAwareStackModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any { return reservationState{Reserved: make(map[int]stackItem)} },
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
				return true, next
			case eventFull:
				return len(current.Reserved)+len(current.Published) == capacity, state
			case eventDequeue:
				if len(current.Published) == 0 {
					return false, state
				}
				last := current.Published[len(current.Published)-1]
				if last.value != event.Item.value {
					return false, state
				}
				next := cloneReservationState(current)
				next.Published = next.Published[:len(next.Published)-1]
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

func cloneReservationState(state reservationState) reservationState {
	reserved := make(map[int]stackItem, len(state.Reserved))
	for id, item := range state.Reserved {
		reserved[id] = item
	}
	return reservationState{
		Reserved: reserved, Published: append([]stackItem(nil), state.Published...),
	}
}
