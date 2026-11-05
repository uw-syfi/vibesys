package workload

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strconv"
	"testing"
)

type randCase struct {
	Seed   uint64     `json:"seed"`
	Labels []any      `json:"labels"`
	Draws  []randDraw `json:"draws"`
}

type randDraw struct {
	Op      string      `json:"op"`
	N       json.Number `json:"n"`
	S       float64     `json:"s"`
	Weights []float64   `json:"weights"`
	Labels  []any       `json:"labels"`
	Want    any         `json:"want"`
}

// labels turns the fixture's labels (strings and json.Numbers) into Rand labels.
func labels(t *testing.T, raw []any) []any {
	t.Helper()
	out := make([]any, len(raw))
	for i, label := range raw {
		switch v := label.(type) {
		case string:
			out[i] = v
		case json.Number:
			n, err := strconv.ParseUint(v.String(), 10, 64)
			if err != nil {
				t.Fatal(err)
			}
			out[i] = n
		default:
			t.Fatalf("label %v", label)
		}
	}
	return out
}

func bits(f float64) string { return fmt.Sprintf("%016x", math.Float64bits(f)) }

// Rand draws exactly what the Python SDK's Rand draws: every case of fixtures/rand.json, written
// by the Python side (fixtures/rand_streams.py), bit for bit.
func TestRandIsThePythonRand(t *testing.T) {
	data, err := os.ReadFile("../../fixtures/rand.json")
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var fixture struct{ Cases []randCase }
	if err := decoder.Decode(&fixture); err != nil {
		t.Fatal(err)
	}
	if len(fixture.Cases) == 0 {
		t.Fatal("no cases")
	}
	for _, c := range fixture.Cases {
		r := NewRand(c.Seed, labels(t, c.Labels)...)
		for i, d := range c.Draws {
			var got string
			switch d.Op {
			case "uint64":
				got = strconv.FormatUint(r.Uint64(), 10)
			case "below":
				n, _ := strconv.Atoi(d.N.String())
				got = strconv.Itoa(r.Below(n))
			case "random":
				got = bits(r.Random())
			case "normal":
				got = bits(r.Normal())
			case "weighted":
				got = strconv.Itoa(r.Weighted(d.Weights))
			case "zipf":
				n, _ := strconv.Atoi(d.N.String())
				got = strconv.Itoa(r.Zipf(n, d.S))
			case "child":
				got = strconv.FormatUint(r.Child(labels(t, d.Labels)...).Uint64(), 10)
			default:
				t.Fatalf("unknown op %q", d.Op)
			}
			if want := fmt.Sprint(d.Want); got != want {
				t.Fatalf("Rand(%d, %v) draw %d %s(%s %v %v): got %s, Python drew %s", c.Seed, c.Labels, i, d.Op, d.N, d.S, d.Weights, got, want)
			}
		}
	}
}

// Property: a child depends on the labels alone, never on what the parent drew, and equals the
// stream of the joined labels.
func TestAChildIsTheStreamOfTheJoinedLabels(t *testing.T) {
	for seed := range uint64(50) {
		parent := NewRand(seed, "p", seed)
		for range seed {
			parent.Uint64()
		}
		child, direct := parent.Child("c", 3), NewRand(seed, "p", seed, "c", 3)
		for range 5 {
			if a, b := child.Uint64(), direct.Uint64(); a != b {
				t.Fatalf("seed %d: child %d, direct %d", seed, a, b)
			}
		}
	}
}

// The ported log and exp are within an ulp or two of Go's math package over the range a Zipf
// table uses (ranks 1..1e7, exponents up to 3).
func TestFdlibmIsAccurate(t *testing.T) {
	for k := 1.0; k < 1e7; k *= 1.37 {
		if got, want := fdlibmLog(k), math.Log(k); math.Abs(got-want) > 4e-16*math.Abs(want) {
			t.Fatalf("log(%v) = %v, want %v", k, got, want)
		}
		for _, s := range []float64{0.3, 0.99, 1.2, 3} {
			x := -s * math.Log(k)
			if got, want := fdlibmExp(x), math.Exp(x); math.Abs(got-want) > 4e-16*want {
				t.Fatalf("exp(%v) = %v, want %v", x, got, want)
			}
		}
	}
}

func TestFsumIsExactlyRounded(t *testing.T) {
	for _, c := range []struct {
		xs   []float64
		want float64
	}{
		{[]float64{0.1, 0.2, 0.3}, 0.6},
		{[]float64{1e100, 1, -1e100}, 1},
		{[]float64{0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1}, 1},
		{[]float64{1, 1e-16, 1e-16}, 1.0000000000000002},
		{nil, 0},
	} {
		if got := fsum(c.xs); got != c.want {
			t.Errorf("fsum(%v) = %v, want %v", c.xs, got, c.want)
		}
	}
}
