package plan_test

import (
	"bytes"
	"errors"
	"math/rand/v2"
	"strings"
	"testing"

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
		if p.Len() != 2 || len(p.Op(0).Steps) != 2 || p.Op(1).Operation != 1 {
			t.Fatalf("plan %+v", p)
		}
		header := p.Op(0).Steps[1].Headers[0].Value.Fill(map[string]plan.Captured{"token": {Text: "abc", JSON: `"abc"`}})
		if header != "Bearer abc" {
			t.Fatalf("header %q", header)
		}
		if got := p.Op(1).Steps[0].Expect; len(got) != 1 || got[0] != 200 {
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
	// The header's count must match the operations that follow.
	var short bytes.Buffer
	writer, _ := files.NewWriter(&short, files.Binpb, files.Header(pb.FileKind_FILE_KIND_REQUESTS, "t"))
	_ = writer.Write(&pb.RequestsHeader{OperationNames: names, Count: 3})
	_ = writer.Write(messages.Op(0, messages.Get("/")))
	_ = writer.Flush()
	cases["count is 3"] = short.Bytes()
	for want, data := range cases {
		_, err := read(t, data, files.Binpb)
		if !errors.Is(err, plan.ErrInvalid) || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: got %v", want, err)
		}
	}
}

// Property: each operation waits for the previous one with its key, and an operation is gated
// exactly when a later one has its key, for any assignment of keys.
func TestExclusiveKeysChainInPlanOrder(t *testing.T) {
	for seed := range uint64(30) {
		random := rand.New(rand.NewPCG(seed, 2))
		keys := make([]string, 1+random.IntN(40))
		var ops []*pb.PlannedOperation
		for i := range keys {
			if random.IntN(3) > 0 {
				keys[i] = string(rune('a' + random.IntN(4)))
			}
			ops = append(ops, messages.Exclusive(messages.Op(0, messages.Get("/")), keys[i]))
		}
		p, err := plan.New([]string{"op"}, ops)
		if err != nil {
			t.Fatal(err)
		}
		for i := range int(p.Len()) {
			op := p.Op(uint64(i))
			want := int64(-1)
			for j := i - 1; j >= 0 && keys[i] != ""; j-- {
				if keys[j] == keys[i] {
					want = int64(j)
					break
				}
			}
			gated := false
			for j := i + 1; j < len(keys) && keys[i] != ""; j++ {
				gated = gated || keys[j] == keys[i]
			}
			if op.After != want || op.Gated != gated {
				t.Fatalf("seed %d op %d keys %v: After %d Gated %v, want %d %v", seed, i, keys, op.After, op.Gated, want, gated)
			}
		}
	}
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
	step := p.Op(0).Steps[1]
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
