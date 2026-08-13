package main

import (
	"cmp"
	"slices"

	"github.com/anishathalye/porcupine"
)

type priorityItem struct {
	value    uint64
	priority uint64
}

func checkPriorityHistory(capacity int, history []recordedOperation) bool {
	operations := make([]porcupine.Operation, 0, len(history))
	for _, op := range history {
		operations = append(operations, op.porcupine())
	}
	return porcupine.CheckOperations(priorityModel(capacity), operations)
}

func priorityModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any { return []priorityItem{} },
		Step: func(state, input, output any) (bool, any) {
			current := state.([]priorityItem)
			in := input.(queueInput)
			out := output.(queueOutput)
			switch in.Kind {
			case "enqueue":
				if in.Value == nil || in.Priority == nil || out.EnqueueOK == nil {
					return false, state
				}
				expected := len(current) < capacity
				if *out.EnqueueOK != expected {
					return false, state
				}
				if !expected {
					return true, state
				}
				next := append([]priorityItem(nil), current...)
				next = append(next, priorityItem{
					value:    *in.Value,
					priority: *in.Priority,
				})
				slices.SortFunc(next, comparePriorityItems)
				return true, next
			case "dequeue":
				if len(current) == 0 {
					return out.DequeueNone && out.DequeueVal == nil, state
				}
				if out.DequeueNone || out.DequeueVal == nil {
					return false, state
				}
				index := slices.IndexFunc(current, func(item priorityItem) bool {
					return item.priority == current[0].priority &&
						item.value == *out.DequeueVal
				})
				if index < 0 {
					return false, state
				}
				next := append([]priorityItem(nil), current...)
				next = slices.Delete(next, index, index+1)
				return true, next
			default:
				return false, state
			}
		},
		Equal: func(first, second any) bool {
			return slices.Equal(first.([]priorityItem), second.([]priorityItem))
		},
	}
}

func comparePriorityItems(first, second priorityItem) int {
	if order := cmp.Compare(first.priority, second.priority); order != 0 {
		return order
	}
	return cmp.Compare(first.value, second.value)
}
