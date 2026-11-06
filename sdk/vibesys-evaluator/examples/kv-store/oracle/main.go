// Command oracle is the kv-store example's accuracy oracle, on the Go SDK's checker (go/check):
// it checks a captured history of the workload in ../generator against the store's semantics.
// Reads must see every acknowledged write at once (bound 0), each key on its own; a write whose
// response was lost may have been applied within loadgen's step timeout, or never.
//
// The task's [accuracy] command runs it on the history the accuracy capture wrote:
//
//	oracle --history DIR [--out /vibesys/artifacts/accuracy]
//
// and it exits 0 (PASS), 1 (FAIL) or 2 (UNKNOWN). Run with KV_STORE_SCALE=test for the
// end-to-end test's scale, as the generator.
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"iter"
	"os"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// minValue and maxValue are the generator's Scale; a written value is drawn from the write's
// seed exactly as the generator drew it.
type scale struct{ minValue, maxValue int }

var (
	example = scale{16, 512}
	test    = scale{4, 32}
)

func (s scale) value(seed uint64) string {
	rand := workload.NewRand(seed)
	out := make([]byte, s.minValue+rand.Below(s.maxValue-s.minValue+1))
	for i := range out {
		out[i] = byte(97 + rand.Below(26))
	}
	return string(out)
}

// data is the dataset file: the seeded values, base64 in the file.
type data struct {
	Values [][]byte `json:"values"`
}

// stored is a key's value as a read shows it: absent (404) or a body.
type stored struct {
	Present bool
	Body    string
}

// op is a piece's input: a read or a write of one key.
type op struct {
	Key   int
	Write bool
	Value stored // a write's
}

// info is the generator's Request info.
type info struct {
	Key  int    `json:"key"`
	Seed uint64 `json:"seed,omitempty"`
}

func spec(d *data, s scale) *check.Spec[check.Register[stored], op, stored] {
	return &check.Spec[check.Register[stored], op, stored]{
		Name:         "kv",
		Project:      project(s),
		EffectWindow: 10 * time.Second, // loadgen's default --timeout per step
		Init: func(keys []string) check.Register[stored] {
			var key int
			fmt.Sscanf(keys[0], "k%d", &key)
			if key < len(d.Values) {
				return check.NewRegister(stored{true, string(d.Values[key])})
			}
			return check.NewRegister(stored{})
		},
		Step: func(r check.Register[stored], o check.Op[op, stored]) ([]check.Register[stored], string) {
			r = r.Prune(o.Floor, 0)
			if o.In.Write {
				written := r.Write(o.In.Value, o.Return)
				if o.Unknown {
					return []check.Register[stored]{r, written}, ""
				}
				return []check.Register[stored]{written}, ""
			}
			if r.MayShow(o.Out, o.Call, 0) {
				return []check.Register[stored]{r}, ""
			}
			return nil, fmt.Sprintf("k%d read %.40q; it may show %d other values", o.In.Key, o.Out.Body, len(r.Allowed(o.Call, 0)))
		},
		Equal: func(a, b check.Register[stored]) bool { return a.Equal(b) },
		Hash:  func(r check.Register[stored]) uint64 { return r.Hash() },
	}
}

// unknown says whether a step's outcome is unknown: no response, or a 5xx (the request may or
// may not have taken effect).
func unknown(s check.Step) bool { return s.Status == 0 || s.Status >= 500 }

// project makes one piece per operation on its key. An operation that stopped at its login (the
// GET or PUT was never sent) touched no key: its piece has none, and an unknown login outcome
// counts toward the check's limit on unknown outcomes like any other.
func project(s scale) func(iter.Seq[[]check.Call], *check.Projection[op, stored]) error {
	return func(phases iter.Seq[[]check.Call], out *check.Projection[op, stored]) error {
		for calls := range phases {
			for i := range calls {
				c := &calls[i]
				var in info
				if err := json.Unmarshal(c.Info, &in); err != nil {
					return fmt.Errorf("%s %d: info: %w", c.Op, c.Index, err)
				}
				if len(c.Steps) < 2 {
					out.Add(check.Piece[op, stored]{Origin: c, Unknown: len(c.Steps) == 0 || unknown(c.Steps[0])})
					continue
				}
				step := c.Steps[1]
				piece := check.Piece[op, stored]{Keys: []string{fmt.Sprintf("k%d", in.Key)}, In: op{Key: in.Key}, Origin: c, Unknown: unknown(step)}
				switch c.Op {
				case "write":
					piece.Write = true
					piece.In.Write, piece.In.Value = true, stored{true, s.value(in.Seed)}
					if !piece.Unknown && step.Status != 200 {
						out.Violation(c, "PUT answered HTTP %d", step.Status)
						continue
					}
				case "read":
					switch {
					case piece.Unknown:
					case step.Status == 200:
						piece.Out = stored{true, string(step.Body)}
					case step.Status == 404:
						piece.Out = stored{}
					default:
						out.Violation(c, "GET answered HTTP %d", step.Status)
						continue
					}
				default:
					return fmt.Errorf("unknown operation %q", c.Op)
				}
				out.Add(piece)
			}
		}
		return nil
	}
}

func main() {
	s := example
	if os.Getenv("KV_STORE_SCALE") == "test" {
		s = test
	}
	check.Main(func(h *check.History) ([]check.AnySpec, error) {
		if h.Dataset == "" {
			return nil, errors.New("the history has no dataset file")
		}
		raw, err := os.ReadFile(h.Dataset)
		if err != nil {
			return nil, err
		}
		d := &data{}
		if err := json.Unmarshal(raw, d); err != nil {
			return nil, fmt.Errorf("%s: %w", h.Dataset, err)
		}
		return []check.AnySpec{spec(d, s)}, nil
	})
}
