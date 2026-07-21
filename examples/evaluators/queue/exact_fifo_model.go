package main

import (
	"slices"

	"github.com/anishathalye/porcupine"
)

func checkExactFIFOHistory(capacity int, history []recordedOperation) bool {
	operations := make([]porcupine.Operation, 0, len(history))
	for _, op := range history {
		operations = append(operations, op.porcupine())
	}
	return porcupine.CheckOperations(exactFIFOModel(capacity), operations)
}

func exactFIFOModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any { return []uint64{} },
		Step: func(state, input, output any) (bool, any) {
			current := state.([]uint64)
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
				next := append([]uint64(nil), current...)
				next = append(next, *in.Value)
				return true, next
			case "dequeue":
				if len(current) == 0 {
					return out.DequeueNone && out.DequeueVal == nil, state
				}
				if out.DequeueNone || out.DequeueVal == nil || *out.DequeueVal != current[0] {
					return false, state
				}
				next := append([]uint64(nil), current[1:]...)
				return true, next
			default:
				return false, state
			}
		},
		Equal: func(first, second any) bool {
			return slices.Equal(first.([]uint64), second.([]uint64))
		},
	}
}
