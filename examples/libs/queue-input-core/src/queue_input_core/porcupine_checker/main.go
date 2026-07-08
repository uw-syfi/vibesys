package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"

	"github.com/anishathalye/porcupine"
)

type historyEntry struct {
	ClientID int64        `json:"client_id"`
	Input    queueInput   `json:"input"`
	Output   queueOutput  `json:"output"`
	Call     int64        `json:"call"`
	Return   int64        `json:"return"`
}

type queueInput struct {
	Kind  string `json:"kind"`
	Value *int   `json:"value,omitempty"`
}

type queueOutput struct {
	EnqueueOK   *bool `json:"enqueue_ok,omitempty"`
	DequeueNone bool  `json:"dequeue_none,omitempty"`
	DequeueVal  *int  `json:"dequeue_value,omitempty"`
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

func queueModel(capacity int) porcupine.Model {
	return porcupine.Model{
		Init: func() any {
			return []int{}
		},
		Step: func(state, input, output any) (bool, any) {
			q := append([]int(nil), state.([]int)...)
			in := input.(queueInput)
			out := output.(queueOutput)
			switch in.Kind {
			case "enqueue":
				if in.Value == nil || out.EnqueueOK == nil {
					return false, state
				}
				expected := len(q) < capacity
				if *out.EnqueueOK != expected {
					return false, state
				}
				if expected {
					q = append(q, *in.Value)
				}
				return true, q
			case "dequeue":
				if len(q) == 0 {
					return out.DequeueNone && out.DequeueVal == nil, q
				}
				if out.DequeueNone || out.DequeueVal == nil || *out.DequeueVal != q[0] {
					return false, state
				}
				return true, q[1:]
			default:
				return false, state
			}
		},
		Equal: func(state1, state2 any) bool {
			a := state1.([]int)
			b := state2.([]int)
			if len(a) != len(b) {
				return false
			}
			for i := range a {
				if a[i] != b[i] {
					return false
				}
			}
			return true
		},
	}
}

func main() {
	historyPath := flag.String("history", "", "Path to JSON history")
	capacity := flag.Int("capacity", 64, "Queue capacity")
	flag.Parse()

	if *historyPath == "" {
		fmt.Fprintln(os.Stderr, "--history is required")
		os.Exit(2)
	}
	if *capacity <= 0 {
		fmt.Fprintln(os.Stderr, "--capacity must be > 0")
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

	if !porcupine.CheckOperations(queueModel(*capacity), ops) {
		fmt.Fprintln(os.Stderr, "history is not linearizable for bounded queue model")
		os.Exit(1)
	}
	fmt.Println("history is linearizable")
}
