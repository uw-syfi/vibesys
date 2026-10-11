package main

import (
	"bytes"
	"fmt"
	"time"

	"github.com/anishathalye/porcupine"
)

// defaultCheckBudget bounds one linearizability decision.
//
// Porcupine's search is worst-case exponential in the number of overlapping
// operations, so a concurrent history is not guaranteed to be decidable in any
// particular time. Deciding a real gate history takes milliseconds, but some
// interleavings of a global sorted-map history are pathological and run for
// minutes. An unbounded check turns those into a hang that the surrounding
// command timeout kills without a diagnostic.
//
// 20s is orders of magnitude above a normal decision, and it keeps the
// benchmark gate (one boundary history plus one concurrent trial) inside the
// framework's 300s benchmark timeout with room for the measured run.
const defaultCheckBudget = 20 * time.Second

func correctnessContract() string {
	return "linearizable point map with weakly consistent ordered operations"
}

func checkOrderedMapHistory(
	history []recordedOperation,
	budget time.Duration,
) porcupine.CheckResult {
	operations := make([]porcupine.Operation, 0, len(history))
	for _, op := range history {
		operations = append(operations, op.porcupine())
	}
	return porcupine.CheckOperationsTimeout(orderedMapModel(), operations, budget)
}

func orderedMapModel() porcupine.Model {
	return porcupine.Model{
		Init: func() any { return &orderedMapSpec{} },
		Step: func(state, input, output any) (bool, any) {
			current := state.(*orderedMapSpec)
			in := input.(mapInput)
			out := output.(mapOutput)
			req, err := requestFromInput(in)
			if err != nil {
				return false, state
			}
			if req.operation == operationRange && !rangeItemsStrictlyIncreasing(out.Items) {
				return false, state
			}
			if in.Weak {
				if err := weakOrderedMatches(req, out); err != nil {
					return false, state
				}
				return true, current
			}
			next := current.clone()
			expected, err := applySpec(next, req)
			if err != nil {
				return false, state
			}
			actual := responseFromOutput(out, req)
			if err := responsesMatch(expected, actual, req); err != nil {
				return false, state
			}
			return true, next
		},
		Equal: func(first, second any) bool {
			return specEqual(first.(*orderedMapSpec), second.(*orderedMapSpec))
		},
	}
}

func requestFromInput(in mapInput) (request, error) {
	switch in.Kind {
	case "put":
		return request{operation: operationPut, key: in.Key, value: in.Value}, nil
	case "get":
		return request{operation: operationGet, key: in.Key}, nil
	case "remove":
		return request{operation: operationRemove, key: in.Key}, nil
	case "min":
		return request{operation: operationMin}, nil
	case "max":
		return request{operation: operationMax}, nil
	case "predecessor":
		return request{operation: operationPredecessor, key: in.Key}, nil
	case "successor":
		return request{operation: operationSuccessor, key: in.Key}, nil
	case "range":
		return request{
			operation: operationRange,
			key:       in.Key,
			value:     in.Value,
			extra:     in.MaxItems,
		}, nil
	default:
		return request{}, fmt.Errorf("unknown map input kind %q", in.Kind)
	}
}

func responseFromOutput(out mapOutput, req request) response {
	resp := response{
		status:    out.Status,
		key:       out.Key,
		value:     out.Value,
		remaining: out.Remaining,
	}
	if req.operation == operationRange {
		resp.items = make([]rangeItem, len(out.Items))
		for index, item := range out.Items {
			resp.items[index] = rangeItem{key: item.Key, value: item.Value}
		}
	}
	return resp
}

func rangeItemsStrictlyIncreasing(items []rangePair) bool {
	for index := 1; index < len(items); index++ {
		if bytes.Compare(items[index-1].Key, items[index].Key) >= 0 {
			return false
		}
	}
	return true
}

func weakOrderedMatches(req request, out mapOutput) error {
	switch req.operation {
	case operationMin, operationMax:
		return weakNeighborStatus(out)
	case operationPredecessor:
		if err := weakNeighborStatus(out); err != nil {
			return err
		}
		if out.Status == statusOK && bytes.Compare(out.Key, req.key) >= 0 {
			return fmt.Errorf("predecessor key is not strictly below the query")
		}
		return nil
	case operationSuccessor:
		if err := weakNeighborStatus(out); err != nil {
			return err
		}
		if out.Status == statusOK && bytes.Compare(out.Key, req.key) <= 0 {
			return fmt.Errorf("successor key is not strictly above the query")
		}
		return nil
	case operationRange:
		return weakRangeMatches(req, out)
	default:
		return fmt.Errorf("weak check on point operation %d", req.operation)
	}
}

func weakNeighborStatus(out mapOutput) error {
	switch out.Status {
	case statusOK, statusMissing:
		return nil
	default:
		return fmt.Errorf("ordered operation status %d", out.Status)
	}
}

func weakRangeMatches(req request, out mapOutput) error {
	if out.Status != statusOK {
		return fmt.Errorf("range status %d", out.Status)
	}
	if !rangeItemsStrictlyIncreasing(out.Items) {
		return fmt.Errorf("range items are not strictly increasing")
	}
	seen := make(map[string]struct{}, len(out.Items))
	for _, item := range out.Items {
		if bytes.Compare(item.Key, req.key) < 0 || bytes.Compare(item.Key, req.value) >= 0 {
			return fmt.Errorf("range item outside [start, end)")
		}
		key := string(item.Key)
		if _, exists := seen[key]; exists {
			return fmt.Errorf("range item duplicates key")
		}
		seen[key] = struct{}{}
	}
	if uint32(len(out.Items)) > req.extra {
		return fmt.Errorf("range count exceeds max_items")
	}
	if !out.Remaining {
		return nil
	}
	if req.extra == 0 {
		if len(out.Items) != 0 {
			return fmt.Errorf("max_items 0 range returned items")
		}
		return nil
	}
	if uint32(len(out.Items)) != req.extra {
		return fmt.Errorf("remaining set below max_items")
	}
	return nil
}
