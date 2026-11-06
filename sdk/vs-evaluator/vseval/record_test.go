package vseval_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/rand/v2"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval"
)

// The bytes of a record are the protocol's, which evaluators and readers that already shipped rely
// on: one object per line, the proto field names as keys, integers as JSON numbers (proto3 JSON
// would write a 64-bit integer as a string), and a key written only when the evaluator set it.
func TestRecordsAreWrittenAsTheProtocolHasAlwaysWrittenThem(t *testing.T) {
	var out bytes.Buffer
	report := vseval.OpenWriter(&out)
	schema := vseval.NewSchema()
	ops := schema.Number("ops", vseval.Unit("ops/s"), vseval.Direction(vseval.Max))
	schema.Number("p99", vseval.Optional())
	bare := schema.Number("bare")
	run, err := report.Declare(schema)
	if err != nil {
		t.Fatal(err)
	}
	run.Set(ops, 41250.3)
	run.Set(bare, 7)
	if err := run.Emit(); err != nil {
		t.Fatal(err)
	}
	want := `{"kind":"hello","protocol":2,"metrics":{"bare":{},"ops":{"unit":"ops/s","direction":"max"},"p99":{"required":false}}}` + "\n" +
		`{"kind":"result","label":"","values":{"bare":7,"ops":41250.3}}` + "\n"
	if out.String() != want {
		t.Errorf("wrote\n%s\nwant\n%s", out.String(), want)
	}

	out.Reset()
	if err := vseval.OpenWriter(&out).EmitError(errors.New("  it failed ")); err != nil {
		t.Fatal(err)
	}
	if want := `{"kind":"error","message":"it failed"}` + "\n"; out.String() != want {
		t.Errorf("wrote %s, want %s", out.String(), want)
	}
}

// Anything the writer is given, it writes as records whose numbers are the numbers it was given,
// found by reading the lines without the writer's types.
func TestWhateverIsReportedIsWrittenWithTheSameNumbers(t *testing.T) {
	for seed := uint64(0); seed < 300; seed++ {
		r := rand.New(rand.NewPCG(seed, 5))
		var out bytes.Buffer
		schema := vseval.NewSchema()
		want := map[string]float64{}
		var handles []vseval.Metric
		for i := 0; i < 1+r.IntN(5); i++ {
			n := fmt.Sprintf("m%d_%d", i, r.IntN(100))
			handles = append(handles, schema.Number(n))
		}
		run, err := vseval.OpenWriter(&out).Declare(schema)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		for _, h := range handles {
			v := r.NormFloat64() * math.Pow(10, float64(r.IntN(40)-20))
			if r.IntN(5) == 0 {
				v = math.Trunc(v)
			}
			want[h.Name()] = v
			run.Set(h, v)
		}
		if err := run.Emit(); err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		lines := strings.Split(strings.TrimSuffix(out.String(), "\n"), "\n")
		if len(lines) != 2 {
			t.Fatalf("seed %d: %d lines\n%s", seed, len(lines), out.String())
		}
		var result struct {
			Kind   string                 `json:"kind"`
			Label  *string                `json:"label"`
			Values map[string]json.Number `json:"values"`
		}
		dec := json.NewDecoder(strings.NewReader(lines[1]))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&result); err != nil {
			t.Fatalf("seed %d: %v\n%s", seed, err, lines[1])
		}
		if result.Kind != "result" || result.Label == nil || *result.Label != "" || len(result.Values) != len(want) {
			t.Fatalf("seed %d: %s", seed, lines[1])
		}
		for name, v := range want {
			got, err := result.Values[name].Float64()
			if err != nil || got != v {
				t.Fatalf("seed %d: %s = %s, wrote %v (%v)", seed, name, result.Values[name], v, err)
			}
		}
	}
}
