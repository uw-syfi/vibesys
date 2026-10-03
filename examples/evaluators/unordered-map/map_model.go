package main

import (
	"sort"
	"time"

	"github.com/anishathalye/porcupine"
)

// defaultCheckBudget bounds one linearizability decision.
//
// Porcupine's search is worst-case exponential in the number of overlapping
// operations, so a concurrent history is not guaranteed to be decidable in any
// particular time. Deciding a real gate history takes milliseconds, but some
// interleavings are pathological and run for minutes. An unbounded check turns
// those into a hang that the surrounding command timeout kills without a
// diagnostic.
//
// 20s is orders of magnitude above a normal decision, and it keeps the
// benchmark gate (one boundary history plus one concurrent trial) inside the
// framework's 300s benchmark timeout with room for the measured run.
const defaultCheckBudget = 20 * time.Second

type register struct {
	present bool
	value   string
}

func correctnessContract() string {
	return "linearizable unordered map"
}

func checkMapHistory(history []recordedOperation, budget time.Duration) porcupine.CheckResult {
	operations := make([]porcupine.Operation, 0, len(history))
	for _, op := range history {
		operations = append(operations, op.porcupine())
	}
	return porcupine.CheckOperationsTimeout(mapModel(), operations, budget)
}

func mapModel() porcupine.Model {
	return porcupine.Model{
		Partition: func(history []porcupine.Operation) [][]porcupine.Operation {
			groups := map[string][]porcupine.Operation{}
			for _, op := range history {
				input := op.Input.(mapInput)
				groups[input.Key] = append(groups[input.Key], op)
			}
			keys := make([]string, 0, len(groups))
			for key := range groups {
				keys = append(keys, key)
			}
			sort.Strings(keys)
			partitions := make([][]porcupine.Operation, 0, len(keys))
			for _, key := range keys {
				partitions = append(partitions, groups[key])
			}
			return partitions
		},
		Init: func() any {
			return register{}
		},
		Step: func(state, input, output any) (bool, any) {
			current := state.(register)
			in := input.(mapInput)
			out := output.(mapOutput)
			switch in.Kind {
			case "put":
				if !out.Ok || out.Missing || len(out.Value) != 0 {
					return false, state
				}
				return true, register{present: true, value: string(in.Value)}
			case "get":
				if !current.present {
					return missingOutput(out), state
				}
				if !presentOutput(out, current.value) {
					return false, state
				}
				return true, state
			case "remove":
				if !current.present {
					return missingOutput(out), state
				}
				if !presentOutput(out, current.value) {
					return false, state
				}
				return true, register{}
			default:
				return false, state
			}
		},
		Equal: func(state1, state2 any) bool {
			return state1.(register) == state2.(register)
		},
	}
}

func missingOutput(out mapOutput) bool {
	return out.Missing && !out.Ok && len(out.Value) == 0
}

func presentOutput(out mapOutput, value string) bool {
	return out.Ok && !out.Missing && string(out.Value) == value
}
