package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"sort"

	"github.com/anishathalye/porcupine"
)

type historyEntry struct {
	ClientID int64      `json:"client_id"`
	Input    kvInput    `json:"input"`
	Output   kvOutput   `json:"output"`
	Call     int64      `json:"call"`
	Return   int64      `json:"return"`
}

type kvInput struct {
	Kind  string `json:"kind"`
	Key   string `json:"key"`
	Value *int   `json:"value,omitempty"`
}

type kvOutput struct {
	Success *bool `json:"success,omitempty"`
	Value   *int  `json:"value,omitempty"`
}

func loadHistory(path string) ([]historyEntry, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read history: %w", err)
	}
	var entries []historyEntry
	if err := json.Unmarshal(data, &entries); err != nil {
		return nil, fmt.Errorf("decode history json: %w", err)
	}
	if len(entries) == 0 {
		return nil, errors.New("history is empty")
	}
	return entries, nil
}

func buildOperations(entries []historyEntry) ([]porcupine.Operation, error) {
	ops := make([]porcupine.Operation, 0, len(entries))
	for idx, entry := range entries {
		if entry.Input.Key == "" {
			return nil, fmt.Errorf("entry %d has empty key", idx)
		}
		if entry.Call > entry.Return {
			return nil, fmt.Errorf("entry %d has call > return", idx)
		}
		ops = append(ops, porcupine.Operation{
			ClientId: int(entry.ClientID),
			Input:    entry.Input,
			Call:     entry.Call,
			Output:   entry.Output,
			Return:   entry.Return,
		})
	}
	return ops, nil
}

func kvModel() porcupine.Model {
	return porcupine.Model{
		Partition: func(history []porcupine.Operation) [][]porcupine.Operation {
			groups := map[string][]porcupine.Operation{}
			for _, op := range history {
				input := op.Input.(kvInput)
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
			return (*int)(nil)
		},
		Step: func(state, input, output any) (bool, any) {
			current := state.(*int)
			in := input.(kvInput)
			out := output.(kvOutput)
			switch in.Kind {
			case "put":
				if in.Value == nil || out.Success == nil || !*out.Success {
					return false, state
				}
				next := *in.Value
				return true, &next
			case "get":
				if current == nil {
					return out.Value == nil, state
				}
				if out.Value == nil || *out.Value != *current {
					return false, state
				}
				return true, state
			case "delete":
				if out.Success == nil {
					return false, state
				}
				expected := current != nil
				if *out.Success != expected {
					return false, state
				}
				return true, (*int)(nil)
			default:
				return false, state
			}
		},
		Equal: func(state1, state2 any) bool {
			a := state1.(*int)
			b := state2.(*int)
			if a == nil || b == nil {
				return a == nil && b == nil
			}
			return *a == *b
		},
	}
}

func main() {
	historyPath := flag.String("history", "", "Path to JSON history")
	flag.Parse()

	if *historyPath == "" {
		fmt.Fprintln(os.Stderr, "--history is required")
		os.Exit(2)
	}

	entries, err := loadHistory(*historyPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	ops, err := buildOperations(entries)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	if !porcupine.CheckOperations(kvModel(), ops) {
		fmt.Fprintln(os.Stderr, "history is not linearizable for kv model")
		os.Exit(1)
	}
	fmt.Println("history is linearizable")
}
