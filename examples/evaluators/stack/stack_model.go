package main

import (
	"slices"

	"github.com/anishathalye/porcupine"
)

type stackItem struct {
	value uint64
}

func checkStackHistory(capacity int, history []recordedOperation) bool {
	operations := make([]porcupine.Operation, 0, len(history))
	for _, op := range history {
		operations = append(operations, op.porcupine())
	}
	return porcupine.CheckOperations(stackModel(capacity), operations)
}

func stackModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any { return []stackItem{} },
		Step: func(state, input, output any) (bool, any) {
			current := state.([]stackItem)
			in := input.(queueInput)
			out := output.(queueOutput)
			switch in.Kind {
			case "enqueue":
				if in.Value == nil || out.EnqueueOK == nil {
					return false, state
				}
				expected := len(current) < capacity
				if *out.EnqueueOK != expected {
					return false, state
				}
				if !expected {
					return true, state
				}
				next := append([]stackItem(nil), current...)
				next = append(next, stackItem{value: *in.Value})
				return true, next
			case "dequeue":
				if len(current) == 0 {
					return out.DequeueNone && out.DequeueVal == nil, state
				}
				if out.DequeueNone || out.DequeueVal == nil {
					return false, state
				}
				last := current[len(current)-1]
				if last.value != *out.DequeueVal {
					return false, state
				}
				next := append([]stackItem(nil), current[:len(current)-1]...)
				return true, next
			default:
				return false, state
			}
		},
		Equal: func(first, second any) bool {
			return slices.Equal(first.([]stackItem), second.([]stackItem))
		},
	}
}
