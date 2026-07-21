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
	Kind  reservationEventKind
	Value uint64
}

type reservationState struct {
	Reserved  map[uint64]struct{}
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
		operation := func(kind reservationEventKind, value uint64, clientID int) porcupine.Operation {
			return porcupine.Operation{
				ClientId: clientID,
				Input:    reservationEvent{Kind: kind, Value: value},
				Call:     op.Call,
				Output:   struct{}{},
				Return:   op.Return,
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
					operation(eventReserve, value, clientBase),
					operation(eventPublish, value, clientBase+1),
				)
			} else {
				operations = append(operations, operation(eventFull, 0, clientBase))
			}
		case "dequeue":
			switch {
			case op.Output.DequeueNone && op.Output.DequeueVal == nil:
				operations = append(operations, operation(eventEmpty, 0, clientBase))
			case !op.Output.DequeueNone && op.Output.DequeueVal != nil:
				operations = append(
					operations,
					operation(eventDequeue, *op.Output.DequeueVal, clientBase),
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
			return reservationState{Reserved: make(map[uint64]struct{})}
		},
		Step: func(state, input, _ any) (bool, any) {
			current := state.(reservationState)
			event := input.(reservationEvent)
			switch event.Kind {
			case eventReserve:
				if len(current.Reserved)+len(current.Published) >= capacity {
					return false, state
				}
				if _, exists := current.Reserved[event.Value]; exists {
					return false, state
				}
				next := cloneReservationState(current)
				next.Reserved[event.Value] = struct{}{}
				return true, next
			case eventPublish:
				if _, exists := current.Reserved[event.Value]; !exists {
					return false, state
				}
				next := cloneReservationState(current)
				delete(next.Reserved, event.Value)
				next.Published = append(next.Published, event.Value)
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
			for value := range a.Reserved {
				if _, exists := b.Reserved[value]; !exists {
					return false
				}
			}
			return true
		},
	}
}

func cloneReservationState(state reservationState) reservationState {
	reserved := make(map[uint64]struct{}, len(state.Reserved))
	for value := range state.Reserved {
		reserved[value] = struct{}{}
	}
	return reservationState{
		Reserved:  reserved,
		Published: append([]uint64(nil), state.Published...),
	}
}
