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
	Value         uint64
}

type reservationState struct {
	Reserved  map[int]uint64
	Published []uint64
}

func checkReservationAwareFIFOHistory(capacity int, history []recordedOperation) bool {
	operations, ok := reservationAwareOperations(history)
	if !ok {
		return false
	}
	return porcupine.CheckOperations(reservationAwareFIFOModel(capacity), operations)
}

func reservationAwareOperations(
	history []recordedOperation,
) ([]porcupine.Operation, bool) {
	operations := make([]porcupine.Operation, 0, len(history)*2)
	for index, op := range history {
		clientBase := index * 2
		operation := func(
			kind reservationEventKind,
			reservationID int,
			value uint64,
			clientID int,
		) porcupine.Operation {
			return porcupine.Operation{
				ClientId: clientID,
				Input: reservationEvent{
					Kind:          kind,
					ReservationID: reservationID,
					Value:         value,
				},
				Call:   op.Call,
				Output: struct{}{},
				Return: op.Return,
			}
		}

		switch op.Input.Kind {
		case "enqueue":
			if op.Input.Value == nil || op.Output.EnqueueOK == nil {
				return nil, false
			}
			value := *op.Input.Value
			if *op.Output.EnqueueOK {
				operations = append(
					operations,
					operation(eventReserve, index, value, clientBase),
					operation(eventPublish, index, value, clientBase+1),
				)
			} else {
				operations = append(operations, operation(eventFull, 0, 0, clientBase))
			}
		case "dequeue":
			switch {
			case op.Output.DequeueNone && op.Output.DequeueVal == nil:
				operations = append(operations, operation(eventEmpty, 0, 0, clientBase))
			case !op.Output.DequeueNone && op.Output.DequeueVal != nil:
				operations = append(
					operations,
					operation(eventDequeue, 0, *op.Output.DequeueVal, clientBase),
				)
			default:
				return nil, false
			}
		default:
			return nil, false
		}
	}
	return operations, true
}

func reservationAwareFIFOModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any {
			return reservationState{Reserved: make(map[int]uint64)}
		},
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
				next.Reserved[event.ReservationID] = event.Value
				return true, next
			case eventPublish:
				reservedValue, exists := current.Reserved[event.ReservationID]
				if !exists || reservedValue != event.Value {
					return false, state
				}
				next := cloneReservationState(current)
				delete(next.Reserved, event.ReservationID)
				next.Published = append(next.Published, reservedValue)
				return true, next
			case eventFull:
				return len(current.Reserved)+len(current.Published) == capacity, state
			case eventDequeue:
				if len(current.Published) == 0 || current.Published[0] != event.Value {
					return false, state
				}
				next := cloneReservationState(current)
				next.Published = append([]uint64(nil), current.Published[1:]...)
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
			for reservationID, value := range a.Reserved {
				if other, exists := b.Reserved[reservationID]; !exists || other != value {
					return false
				}
			}
			return true
		},
	}
}

func cloneReservationState(state reservationState) reservationState {
	reserved := make(map[int]uint64, len(state.Reserved))
	for reservationID, value := range state.Reserved {
		reserved[reservationID] = value
	}
	return reservationState{
		Reserved:  reserved,
		Published: append([]uint64(nil), state.Published...),
	}
}
