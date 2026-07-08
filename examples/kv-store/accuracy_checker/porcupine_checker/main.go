// Porcupine linearizability checker for the RESP2 KV store.
//
// Reads a concurrent operation history (captured over the wire by checker.py)
// and verifies it is linearizable against a sequential model of the Redis
// subset the target implements. The model is partitioned per key, so each key
// is checked independently — keys are namespaced by type (string vs hash) by
// the recorder, so a given key only ever sees one op family.
//
// Usage: go run . --history <path.json>   (exit 0 = linearizable)
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
	ClientID int64    `json:"client_id"`
	Input    kvInput  `json:"input"`
	Output   kvOutput `json:"output"`
	Call     int64    `json:"call"`
	Return   int64    `json:"return"`
}

type kvInput struct {
	Kind   string            `json:"kind"`
	Key    string            `json:"key"`
	Value  *string           `json:"value,omitempty"`  // set
	Fields map[string]string `json:"fields,omitempty"` // hset
}

type kvOutput struct {
	OK      *bool             `json:"ok,omitempty"`      // set
	Value   *string           `json:"value,omitempty"`   // get (nil = missing)
	Existed *int              `json:"existed,omitempty"` // del (0 or 1)
	Added   *int              `json:"added,omitempty"`   // hset (# new fields)
	Hash    map[string]string `json:"hash,omitempty"`    // hgetall
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

func mapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if bv, ok := b[k]; !ok || bv != v {
			return false
		}
	}
	return true
}

// kvModel is the sequential spec. State per key is one of:
//   - nil                 absent
//   - string              a plain string value (SET/GET/DEL)
//   - map[string]string   a hash (HSET/HGETALL)
func kvModel() porcupine.Model {
	return porcupine.Model{
		Partition: func(history []porcupine.Operation) [][]porcupine.Operation {
			groups := map[string][]porcupine.Operation{}
			for _, op := range history {
				groups[op.Input.(kvInput).Key] = append(groups[op.Input.(kvInput).Key], op)
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
		Init: func() any { return nil },
		Step: func(state, input, output any) (bool, any) {
			in := input.(kvInput)
			out := output.(kvOutput)
			switch in.Kind {
			case "set":
				if in.Value == nil || out.OK == nil || !*out.OK {
					return false, state
				}
				return true, *in.Value
			case "get":
				if state == nil {
					return out.Value == nil, state
				}
				cur, ok := state.(string)
				return ok && out.Value != nil && *out.Value == cur, state
			case "del":
				if out.Existed == nil {
					return false, state
				}
				expected := 0
				if state != nil {
					expected = 1
				}
				if *out.Existed != expected {
					return false, state
				}
				return true, nil
			case "hset":
				if in.Fields == nil || out.Added == nil {
					return false, state
				}
				cur, _ := state.(map[string]string)
				next := make(map[string]string, len(cur)+len(in.Fields))
				for k, v := range cur {
					next[k] = v
				}
				added := 0
				for f, v := range in.Fields {
					if _, exists := next[f]; !exists {
						added++
					}
					next[f] = v
				}
				if *out.Added != added {
					return false, state
				}
				return true, next
			case "hgetall":
				cur, _ := state.(map[string]string)
				return mapsEqual(cur, out.Hash), state
			default:
				return false, state
			}
		},
		Equal: func(s1, s2 any) bool {
			switch a := s1.(type) {
			case nil:
				return s2 == nil
			case string:
				b, ok := s2.(string)
				return ok && a == b
			case map[string]string:
				b, ok := s2.(map[string]string)
				return ok && mapsEqual(a, b)
			}
			return false
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
		fmt.Fprintln(os.Stderr, "history is NOT linearizable for the kv model")
		os.Exit(1)
	}
	fmt.Println("history is linearizable")
}
