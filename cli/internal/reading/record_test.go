package reading_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
	"unicode"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/reflect/protoreflect"

	evaluatorv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/cli/internal/reading"
)

// write is what an SDK writes a record as: the generated message in proto3 JSON with the proto
// field names, on one line.
func write(t testing.TB, rec *evaluatorv1.ResultRecord) string {
	t.Helper()
	raw, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(rec)
	if err != nil {
		t.Fatalf("marshal %v: %v", rec, err)
	}
	var line bytes.Buffer
	if err := json.Compact(&line, raw); err != nil {
		t.Fatalf("compact %s: %v", raw, err)
	}
	return line.String()
}

func ptr[T any](v T) *T { return &v }

// Anything an SDK writes through the generated message, the reader accepts with the same number.
func TestWhatTheGeneratedWriterWritesReadsBackTheSameNumber(t *testing.T) {
	for seed := uint64(0); seed < 500; seed++ {
		r := rand.New(rand.NewPCG(seed, 23))
		metrics := map[string]*evaluatorv1.MetricSpec{}
		values := map[string]float64{}
		want := 1 + r.IntN(5)
		var scored string
		for len(metrics) < want {
			n := name(r)
			if _, dup := metrics[n]; dup {
				continue
			}
			spec := &evaluatorv1.MetricSpec{}
			if r.IntN(2) == 0 {
				spec.Unit = ptr("u")
			}
			if r.IntN(2) == 0 {
				spec.Direction = ptr([]string{"max", "min"}[r.IntN(2)])
			}
			metrics[n] = spec
			values[n] = number(r)
			scored = n
		}
		hello := &evaluatorv1.ResultRecord{Kind: ptr("hello"), Protocol: ptr(uint32(2)), Metrics: metrics}
		result := &evaluatorv1.ResultRecord{Kind: ptr("result"), Label: ptr(""), Values: values}
		got, err := reading.Protocol2([]byte(write(t, hello)+"\n"+write(t, result)+"\n"), scored)
		if err != nil {
			t.Fatalf("seed %d: %v\n%s\n%s", seed, err, write(t, hello), write(t, result))
		}
		if got.Value != values[scored] {
			t.Fatalf("seed %d: read %v, wrote %v", seed, got.Value, values[scored])
		}
		for _, o := range got.Others {
			if o.Value != values[o.Name] {
				t.Fatalf("seed %d: read %s = %v, wrote %v", seed, o.Name, o.Value, values[o.Name])
			}
		}
	}
}

// What an error record writes through the generated message is a reported error, with its
// requirement, and never a number.
func TestWhatTheGeneratedWriterWritesForAnErrorIsReported(t *testing.T) {
	for _, c := range []struct {
		rec         *evaluatorv1.ResultRecord
		requirement string
	}{
		{&evaluatorv1.ResultRecord{Kind: ptr("error"), Message: ptr("x")}, ""},
		{&evaluatorv1.ResultRecord{Kind: ptr("error"), Message: ptr("x"), ErrorKind: ptr("transient")}, ""},
		{&evaluatorv1.ResultRecord{Kind: ptr("error"), Message: ptr("x"), ErrorKind: ptr("requirement_not_met"), Requirement: ptr("prefix_cache")}, "prefix_cache"},
		{&evaluatorv1.ResultRecord{Kind: ptr("error"), Message: ptr("x"), Partial: &evaluatorv1.PartialResult{
			Name: ptr("warmup"), Value: ptr(16.4), Direction: ptr("max"), Unit: ptr("tok/s"), Target: ptr(79.7),
			Progress: &evaluatorv1.Progress{Completed: ptr(uint32(15)), Required: ptr(uint32(72)), Unit: ptr("rounds")},
		}}, ""},
	} {
		_, err := reading.Protocol2([]byte(write(t, c.rec)), "m")
		var reported *reading.Reported
		if !errors.As(err, &reported) || reported.Requirement != c.requirement {
			t.Errorf("%s: %v, want a reported error with requirement %q", write(t, c.rec), err, c.requirement)
		}
	}
}

// Every key of the definition, given a value of another JSON type, is rejected and named. Nothing
// the reader accepts has a number it guessed.
func TestAKeyOfTheWrongTypeIsRejectedAndNamed(t *testing.T) {
	hello := `{"kind":"hello","protocol":2,"metrics":{"m":{}}}` + "\n"
	wrong := []string{`"x"`, `true`, `[1]`, `{"a":1}`, `1.5`, `-1`, `1e999`}
	templates := map[string]string{
		"protocol":                   `{"kind":"hello","protocol":%s,"metrics":{"m":{}}}`,
		"metrics":                    `{"kind":"hello","protocol":2,"metrics":%s}`,
		"unit":                       `{"kind":"hello","protocol":2,"metrics":{"m":{"unit":%s}}}`,
		"direction":                  `{"kind":"hello","protocol":2,"metrics":{"m":{"direction":%s}}}`,
		"required":                   `{"kind":"hello","protocol":2,"metrics":{"m":{"required":%s}}}`,
		"label":                      hello + `{"kind":"result","label":%s,"values":{"m":1}}`,
		"values":                     hello + `{"kind":"result","values":%s}`,
		"message":                    `{"kind":"error","message":%s}`,
		"error_kind":                 `{"kind":"error","message":"x","error_kind":%s}`,
		"requirement":                `{"kind":"error","message":"x","error_kind":"requirement_not_met","requirement":%s}`,
		"partial":                    `{"kind":"error","message":"x","partial":%s}`,
		"partial.name":               `{"kind":"error","message":"x","partial":{"name":%s,"value":1,"direction":"max"}}`,
		"partial.value":              `{"kind":"error","message":"x","partial":{"name":"n","value":%s,"direction":"max"}}`,
		"partial.direction":          `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":%s}}`,
		"partial.unit":               `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","unit":%s}}`,
		"partial.target":             `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","target":%s}}`,
		"partial.progress":           `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","progress":%s}}`,
		"partial.progress.completed": `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","progress":{"completed":%s,"required":3,"unit":"u"}}}`,
		"partial.progress.required":  `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","progress":{"completed":0,"required":%s,"unit":"u"}}}`,
		"partial.progress.unit":      `{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","progress":{"completed":0,"required":3,"unit":%s}}}`,
	}
	// A wrong value that happens to be right for the key (a string for a string) is not a case.
	fine := map[string][]string{
		"unit": {`"x"`}, "message": {`"x"`}, "partial.unit": {`"x"`},
		"partial.name": {`"x"`}, "partial.progress.unit": {`"x"`}, "requirement": {`"x"`},
		"partial.value": {`1.5`, `-1`}, "partial.target": {`1.5`, `-1`},
		"required": {`true`}, "protocol": {},
	}
	for key, tmpl := range templates {
		for _, v := range wrong {
			if slicesContains(fine[key], v) {
				continue
			}
			stream := fmt.Sprintf(tmpl, v)
			got, err := reading.Protocol2([]byte(stream), "m")
			var rej *reading.Rejection
			if errors.As(err, &rej) {
				last := key[strings.LastIndex(key, ".")+1:]
				if !strings.Contains(rej.Message, last) {
					t.Errorf("%s = %s: %q does not name the key", key, v, rej)
				}
				continue
			}
			t.Errorf("%s = %s: read %+v, %v; want a rejection", key, v, got, err)
		}
	}
}

func slicesContains(list []string, s string) bool {
	for _, e := range list {
		if e == s {
			return true
		}
	}
	return false
}

// A name holds no white space, in every place the definition says so: a declared metric (checked
// by the reader) and a requirement, a partial's name and a unit of progress (checked by the
// definition's pattern). The two agree on every rune, so there is one meaning of white space.
func TestEveryPlaceThatForbidsWhiteSpaceAgrees(t *testing.T) {
	isSpace := func(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }
	for r := rune(1); r < 0x3200; r++ {
		if r >= 0xd800 && r < 0xe000 {
			continue
		}
		bad := isSpace(r)
		s := "a" + string(r) + "b"
		encoded, _ := json.Marshal(s)
		cases := map[string]string{
			"metric name": fmt.Sprintf(`{"kind":"hello","protocol":2,"metrics":{%s:{}}}`, encoded),
			"requirement": fmt.Sprintf(`{"kind":"error","message":"x","error_kind":"requirement_not_met","requirement":%s}`, encoded),
			"partial":     fmt.Sprintf(`{"kind":"error","message":"x","partial":{"name":%s,"value":1,"direction":"max"}}`, encoded),
			"unit":        fmt.Sprintf(`{"kind":"error","message":"x","partial":{"name":"n","value":1,"direction":"max","progress":{"completed":0,"required":1,"unit":%s}}}`, encoded),
		}
		for place, stream := range cases {
			_, err := reading.Protocol2([]byte(stream), "m")
			var rej *reading.Rejection
			rejected := errors.As(err, &rej)
			if place == "metric name" {
				// A hello alone has no outcome: it is rejected either way, for a different reason.
				rejected = rejected && rej.Code == reading.InvalidMetricName
			}
			if rejected != bad {
				t.Fatalf("%s with U+%04X: rejected %v, white space %v (%v)", place, r, rejected, bad, err)
			}
		}
	}
}

// The reader handles every kind of field the definition has. A new kind of field is a decision for
// the reader's author, so this fails when the definition grows one.
func TestEveryFieldOfTheDefinitionIsOneTheReaderHandles(t *testing.T) {
	handled := map[protoreflect.Kind]bool{
		protoreflect.StringKind: true, protoreflect.BoolKind: true, protoreflect.Uint32Kind: true,
		protoreflect.DoubleKind: true, protoreflect.MessageKind: true,
	}
	var walk func(protoreflect.MessageDescriptor)
	seen := map[protoreflect.FullName]bool{}
	walk = func(md protoreflect.MessageDescriptor) {
		if seen[md.FullName()] || md.IsMapEntry() {
			return
		}
		seen[md.FullName()] = true
		for i := 0; i < md.Fields().Len(); i++ {
			fd := md.Fields().Get(i)
			if fd.IsMap() {
				fd = fd.MapValue()
			}
			if fd.IsList() || !handled[fd.Kind()] {
				t.Errorf("%s: kind %s (list %v) is not handled by the reader", fd.FullName(), fd.Kind(), fd.IsList())
			}
			if fd.Kind() == protoreflect.MessageKind {
				walk(fd.Message())
			}
		}
	}
	walk((&evaluatorv1.ResultRecord{}).ProtoReflect().Descriptor())
}
