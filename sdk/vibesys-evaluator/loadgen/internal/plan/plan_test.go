package plan_test

import (
	"bytes"
	"context"
	"errors"
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/messages"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
)

func read(t *testing.T, data []byte, format files.Format) (*plan.Plan, error) {
	t.Helper()
	return plan.Read(bytes.NewReader(data), format)
}

func TestReadCompilesEveryOperationInBothFormats(t *testing.T) {
	login := messages.CaptureJSON(messages.Post(messages.Text(`{"user":"u"}`), "/login"), "token", "/token")
	use := messages.Header(messages.Get("/kv/", messages.Raw("token")), "Authorization", "Bearer ", messages.Raw("token"))
	ops := []*pb.PlannedOperation{messages.Op(0, login, messages.Expect(use, 200, 404)), messages.Op(1, messages.Get("/x"))}
	for _, format := range []files.Format{files.Binpb, files.JSON} {
		p, err := read(t, messages.RequestsFile(format, []string{"read", "other"}, ops...), format)
		if err != nil {
			t.Fatal(err)
		}
		if len(p.Ops) != 2 || len(p.Ops[0].Steps) != 2 || p.Ops[1].Operation != 1 {
			t.Fatalf("plan %+v", p)
		}
		header := p.Ops[0].Steps[1].Headers[0].Value.Fill(map[string]plan.Captured{"token": {Text: "abc", JSON: `"abc"`}})
		if header != "Bearer abc" {
			t.Fatalf("header %q", header)
		}
		if got := p.Ops[1].Steps[0].Expect; len(got) != 1 || got[0] != 200 {
			t.Fatalf("default expect %v", got)
		}
	}
}

func TestReadRejectsPlansItCannotRun(t *testing.T) {
	names := []string{"a"}
	thrift := &pb.PlannedOperation{Steps: []*pb.Step{{Call: &pb.Step_Thrift{Thrift: &pb.ThriftCall{}}}}}
	cases := map[string][]byte{
		"names no capture of an earlier step": messages.RequestsFile(files.Binpb, names, messages.Op(0, messages.Get("/", messages.Raw("token")))),
		"outside operation_names":             messages.RequestsFile(files.Binpb, names, messages.Op(3, messages.Get("/"))),
		"only HTTP calls":                     messages.RequestsFile(files.Binpb, names, thrift),
		"steps":                               messages.RequestsFile(files.Binpb, names, messages.Op(0)),
		"unsupported schema version":          messages.RequestsFileVersion(files.Binpb, 2, names, messages.Op(0, messages.Get("/"))),
		"literal \"/\"":                       messages.RequestsFile(files.Binpb, names, messages.Op(0, messages.Get("x"))),
		"operation_names":                     messages.RequestsFile(files.Binpb, []string{"Bad-Name"}, messages.Op(0, messages.Get("/"))),
	}
	// A capture of the same step does not count: refs resolve against earlier steps only.
	self := messages.CaptureJSON(messages.Get("/", messages.Raw("t")), "t", "/t")
	cases["\"t\" names no capture"] = messages.RequestsFile(files.Binpb, names, messages.Op(0, self))
	for want, data := range cases {
		_, err := read(t, data, files.Binpb)
		if !errors.Is(err, plan.ErrInvalid) || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: got %v", want, err)
		}
	}
}

// stream opens a Stream over a whole encoded request stream (binpb).
func stream(t *testing.T, data []byte, buffer int) *plan.Stream {
	t.Helper()
	s, err := plan.OpenStream(bytes.NewReader(data), buffer, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Stop(); s.Wait() })
	return s
}

// takeAll takes operations until the stream fails, and returns them with the error.
func takeAll(t *testing.T, s *plan.Stream) ([]*plan.Op, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute) // hang guard only
	defer cancel()
	var ops []*plan.Op
	for {
		op, _, err := s.Take(ctx)
		if err != nil {
			return ops, err
		}
		ops = append(ops, op)
	}
}

// Property: a stream's operations come out in stream order, indexed from 0, and each waits for
// the previous one with its key (Prev is that one's Done), for any assignment of keys and any
// buffer, across the compile workers' batches.
func TestAStreamChainsExclusiveKeysInStreamOrder(t *testing.T) {
	for seed := range uint64(30) {
		random := rand.New(rand.NewPCG(seed, 2))
		keys := make([]string, 1+random.IntN(600))
		var ops []*pb.PlannedOperation
		for i := range keys {
			if random.IntN(3) > 0 {
				keys[i] = string(rune('a' + random.IntN(4)))
			}
			ops = append(ops, messages.Exclusive(messages.Op(0, messages.Get("/"+strconv.Itoa(i))), keys[i]))
		}
		got, err := takeAll(t, stream(t, messages.RequestsFile(files.Binpb, []string{"op"}, ops...), 1+random.IntN(64)))
		if !errors.Is(err, plan.ErrStreamEnded) || len(got) != len(keys) {
			t.Fatalf("seed %d: %d of %d operations, then %v", seed, len(got), len(keys), err)
		}
		last := map[string]chan struct{}{}
		for i, op := range got {
			if op.Index != uint64(i) || op.Steps[0].Path.Fill(nil) != "/"+strconv.Itoa(i) {
				t.Fatalf("seed %d: operation %d is %d (%s)", seed, i, op.Index, op.Steps[0].Path.Fill(nil))
			}
			if (op.Done != nil) != (keys[i] != "") || (keys[i] != "" && op.Prev != (<-chan struct{})(last[keys[i]])) {
				t.Fatalf("seed %d op %d key %q: Prev %v Done %v", seed, i, keys[i], op.Prev, op.Done)
			}
			if keys[i] != "" {
				last[keys[i]] = op.Done
			}
		}
	}
}

// An operation loadgen cannot run fails the stream when it is reached, naming its index, after
// the operations before it.
func TestAStreamFailsAtAnInvalidOperation(t *testing.T) {
	ops := []*pb.PlannedOperation{messages.Op(0, messages.Get("/")), messages.Op(0, messages.Get("/")), messages.Op(0, messages.Get("/", messages.Raw("token")))}
	got, err := takeAll(t, stream(t, messages.RequestsFile(files.Binpb, []string{"op"}, ops...), 8))
	if len(got) != 2 || !errors.Is(err, plan.ErrInvalid) || !strings.Contains(err.Error(), "operation 2") {
		t.Fatalf("%d operations, then %v", len(got), err)
	}
}

// A stream's headers are checked before the phase starts.
func TestOpenStreamChecksTheHeaders(t *testing.T) {
	op := messages.Op(0, messages.Get("/"))
	cases := map[string][]byte{
		"unsupported schema version": messages.RequestsFileVersion(files.Binpb, 2, []string{"a"}, op),
		"operation_names":            messages.RequestsFile(files.Binpb, []string{"Bad-Name"}, op),
		"FileHeader.kind":            encodeOne(t, pb.FileKind_FILE_KIND_SUMMARY),
	}
	for want, data := range cases {
		if _, err := plan.OpenStream(bytes.NewReader(data), 8, time.Now()); !errors.Is(err, plan.ErrInvalid) || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: got %v", want, err)
		}
	}
	if _, err := plan.OpenStream(bytes.NewReader(nil), 8, time.Now()); !errors.Is(err, plan.ErrStreamEnded) || errors.Is(err, plan.ErrInvalid) {
		t.Errorf("empty stream: got %v", err)
	}
}

func encodeOne(t *testing.T, kind pb.FileKind) []byte {
	t.Helper()
	var out bytes.Buffer
	writer, err := files.NewWriter(&out, files.Binpb, files.Header(kind, "t"))
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.Flush(); err != nil {
		t.Fatal(err)
	}
	return out.Bytes()
}

func TestTextFillEncodesRefs(t *testing.T) {
	captured := map[string]plan.Captured{"s": {Text: `a"b`, JSON: `"a\"b"`}, "n": {Text: "12", JSON: "12"}}
	ops := []*pb.PlannedOperation{messages.Op(0,
		messages.CaptureJSON(messages.CaptureJSON(messages.Get("/"), "s", "/s"), "n", "/n"),
		messages.Post(messages.Text(`{"s":"`, messages.Ref{Name: "s", Encoding: pb.RefEncoding_REF_ENCODING_JSON_STRING},
			`","v":`, messages.Ref{Name: "s", Encoding: pb.RefEncoding_REF_ENCODING_JSON_VALUE},
			`,"n":`, messages.Ref{Name: "n", Encoding: pb.RefEncoding_REF_ENCODING_JSON_VALUE}, `}`), "/", messages.Raw("s")),
	)}
	p, err := plan.New([]string{"op"}, ops)
	if err != nil {
		t.Fatal(err)
	}
	step := p.Ops[0].Steps[1]
	if got := step.Body.Text.Fill(captured); got != `{"s":"a\"b","v":"a\"b","n":12}` {
		t.Fatalf("body %s", got)
	}
	if got := step.Path.Fill(captured); got != `/a"b` {
		t.Fatalf("path %s", got)
	}
}

// Property: a pointer built from tokens parses back to them, escapes included.
func TestParsePointerInvertsEscaping(t *testing.T) {
	alphabet := []string{"a", "~", "/", "0", "1", "", "é"}
	for seed := range uint64(200) {
		random := rand.New(rand.NewPCG(seed, 3))
		tokens := make([]string, 1+random.IntN(4))
		var pointer strings.Builder
		for i := range tokens {
			for range random.IntN(4) {
				tokens[i] += alphabet[random.IntN(len(alphabet))]
			}
			pointer.WriteString("/" + strings.ReplaceAll(strings.ReplaceAll(tokens[i], "~", "~0"), "/", "~1"))
		}
		got, err := plan.ParsePointer(pointer.String())
		if err != nil || strings.Join(got, "\x00") != strings.Join(tokens, "\x00") {
			t.Fatalf("%q: got %q, %v; want %q", pointer.String(), got, err, tokens)
		}
	}
	for _, bad := range []string{"a", "/~2", "/x~"} {
		if _, err := plan.ParsePointer(bad); err == nil {
			t.Fatalf("%q parsed", bad)
		}
	}
}
