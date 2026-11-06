package conformance_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/messages"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/summary"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/version"
)

var update = flag.Bool("update", false, "rewrite the fixtures")

const dir = "../../../fixtures"

// Entry is one fixture in manifest.json, the index both languages' tests read.
type Entry struct {
	File        string `json:"file"`
	Kind        string `json:"kind"`
	Format      string `json:"format"`
	Description string `json:"description"`
	// Valid files decode, validate and hold Messages messages after the header; an invalid one is
	// rejected by the reader with an error containing Error.
	Valid    bool   `json:"valid"`
	Error    string `json:"error,omitempty"`
	Messages int    `json:"messages,omitempty"`
	// Summary fixtures: what decoding the histograms must give (microseconds, HdrHistogram's
	// value at a percentile), and the results fixture the summary is computed from.
	LatencyP50Micros int64  `json:"latency_p50_micros,omitempty"`
	LatencyP99Micros int64  `json:"latency_p99_micros,omitempty"`
	LatencyCount     int64  `json:"latency_count,omitempty"`
	FromResults      string `json:"from_results,omitempty"`
	// Histograms: what decoding each histogram of a summary or selftest fixture must give, by
	// field path ("latency", "operations.2.latency", "added_latency", ...).
	Histograms map[string]HistogramExpect `json:"histograms,omitempty"`
}

// HistogramExpect is one decoded histogram: its total count and HdrHistogram's value at the 50th
// and 99th percentile, in microseconds.
type HistogramExpect struct {
	Count     int64 `json:"count"`
	P50Micros int64 `json:"p50_micros"`
	P99Micros int64 `json:"p99_micros"`
	MaxMicros int64 `json:"max_micros"`
}

// histograms names every histogram of a summary or selftest message.
func histograms(message proto.Message) map[string]*pb.HdrHistogram {
	switch m := message.(type) {
	case *pb.PhaseSummary:
		out := map[string]*pb.HdrHistogram{"latency": m.GetLatency(), "send_lag": m.GetSendLag(), "exclusive_wait": m.GetExclusiveWait(),
			"generator.wait": m.GetGenerator().GetWait()}
		for i, op := range m.GetOperations() {
			out["operations."+strconv.Itoa(i)+".latency"] = op.GetLatency()
		}
		return out
	case *pb.SelftestResult:
		return map[string]*pb.HdrHistogram{"added_latency": m.GetAddedLatency()}
	}
	return nil
}

func expect(t *testing.T, message proto.Message) map[string]HistogramExpect {
	t.Helper()
	out := map[string]HistogramExpect{}
	for name, h := range histograms(message) {
		decoded, err := summary.Decode(h)
		if err != nil {
			t.Fatal(err)
		}
		out[name] = HistogramExpect{Count: decoded.TotalCount(), P50Micros: decoded.ValueAtQuantile(50), P99Micros: decoded.ValueAtQuantile(99), MaxMicros: decoded.Max()}
	}
	return out
}

// Manifest is manifest.json.
type Manifest struct {
	About   string  `json:"about"`
	Entries []Entry `json:"entries"`
}

const base = int64(1_700_000_000_000_000_000)
const ms = int64(1_000_000)

var names = []string{"login_read", "search", "put_value", "delete_value"}

func requests() []*pb.PlannedOperation {
	login := messages.CaptureJSON(messages.Post(messages.Text(`{"user":"u1","password":"p1"}`), "/login"), "token", "/token")
	messages.Header(login, "Content-Type", "application/json")
	read := messages.Header(messages.Get("/kv/", messages.Raw("token")), "Authorization", "Bearer ", messages.Raw("token"))
	messages.Expect(read, 200, 404)
	echo := messages.Post(messages.Text(`{"token":"`, messages.Ref{Name: "token", Encoding: pb.RefEncoding_REF_ENCODING_JSON_STRING},
		`","as_value":`, messages.Ref{Name: "token", Encoding: pb.RefEncoding_REF_ENCODING_JSON_VALUE}, `}`), "/echo")
	search := messages.Query(messages.Query(messages.Get("/hotels"), "inDate", "2015-04-09"), "lat", "37.7867")
	form := messages.HTTP(pb.HttpMethod_HTTP_METHOD_POST, "/user")
	form.GetHttp().Body = &pb.HttpCall_Form{Form: &pb.Form{Fields: []*pb.Param{
		{Name: "username", Value: messages.Text("Cornell_1")}, {Name: "password", Value: messages.Text("1111111111")},
	}}}
	put := messages.HTTP(pb.HttpMethod_HTTP_METHOD_PUT, "/kv/k7")
	put.GetHttp().Body = &pb.HttpCall_Raw{Raw: []byte{0, 1, 2, 255}}
	session := &pb.Capture{Name: "session", Source: &pb.Capture_Header{Header: "X-Session"}}
	put.Captures = append(put.Captures, session)
	confirm := messages.Header(messages.Get("/kv/k7"), "X-Session", messages.Raw("session"))
	del := messages.HTTP(pb.HttpMethod_HTTP_METHOD_DELETE, "/kv/k7")
	keep := func(op *pb.PlannedOperation) *pb.PlannedOperation { op.KeepBodies = true; return op }
	return []*pb.PlannedOperation{
		keep(messages.Op(0, login, read, echo)),
		messages.Op(1, search),
		messages.Op(1, form),
		keep(messages.Exclusive(messages.Op(2, put, confirm), "k7")),
		keep(messages.Exclusive(messages.Op(3, del), "k7")),
	}
}

func digest(body string) *pb.StepResult_Digest {
	sum := sha256.Sum256([]byte(body))
	return &pb.StepResult_Digest{Digest: &pb.BodyDigest{Sha256: sum[:], Length: uint64(len(body))}}
}

// window is the summary fixture's measurement window: [base+1s, base+2s).
var window = summary.Window{Start: base + 1000*ms, End: base + 2000*ms}

type stepAt struct {
	start, end int64
	status     uint32
	response   any
	err        pb.ErrorKind
	message    string
}

func steps(list ...stepAt) []*pb.StepResult {
	var out []*pb.StepResult
	for _, s := range list {
		step := &pb.StepResult{StartUnixNanos: base + s.start, EndUnixNanos: base + s.end, Status: s.status, Error: s.err, ErrorMessage: s.message}
		switch r := s.response.(type) {
		case string:
			step.Response = &pb.StepResult_Body{Body: []byte(r)}
		case *pb.StepResult_Digest:
			step.Response = r
		}
		out = append(out, step)
	}
	return out
}

// results is a results file, in completion order, with Picked times for the summary.
func results() []summary.Record {
	at := func(v int64) int64 { return base + v }
	return []summary.Record{
		// Warmup: not in the summary.
		{Picked: at(500 * ms), Result: &pb.OperationResult{Index: 0, Operation: 0, ScheduledUnixNanos: at(500 * ms),
			StartUnixNanos: at(500 * ms), EndUnixNanos: at(512 * ms), Steps: steps(
				stepAt{500 * ms, 505 * ms, 200, `{"token":"tk-1"}`, 0, ""},
				stepAt{505 * ms, 509 * ms, 404, "", 0, ""},
				stepAt{509 * ms, 512 * ms, 200, `{"token":"tk-1","as_value":"tk-1"}`, 0, ""})}},
		{Picked: at(1001 * ms), Result: &pb.OperationResult{Index: 1, Operation: 1, ScheduledUnixNanos: at(1000 * ms),
			StartUnixNanos: at(1001 * ms), EndUnixNanos: at(1004 * ms), Steps: steps(stepAt{1001 * ms, 1004 * ms, 200, digest(`{"hotels":[]}`), 0, ""})}},
		{Picked: at(1200 * ms), GeneratorWait: 3 * time.Millisecond, Result: &pb.OperationResult{Index: 2, Operation: 1, ScheduledUnixNanos: at(1200 * ms),
			StartUnixNanos: at(1200 * ms), EndUnixNanos: at(1230 * ms), Error: pb.ErrorKind_ERROR_KIND_STATUS,
			Steps: steps(stepAt{1200 * ms, 1230 * ms, 500, digest("internal error"), pb.ErrorKind_ERROR_KIND_STATUS, ""})}},
		{Picked: at(1400 * ms), Result: &pb.OperationResult{Index: 3, Operation: 2, ScheduledUnixNanos: at(1400 * ms),
			StartUnixNanos: at(1400 * ms), EndUnixNanos: at(1420 * ms), Steps: steps(
				stepAt{1400 * ms, 1410 * ms, 200, "", 0, ""}, stepAt{1410 * ms, 1420 * ms, 200, "\x00\x01\x02\xff", 0, ""})}},
		// Waited 15 ms for key k7 (exclusive wait), then ran past the window's end (spilled).
		{Picked: at(1990 * ms), Result: &pb.OperationResult{Index: 4, Operation: 3, ScheduledUnixNanos: at(1990 * ms),
			StartUnixNanos: at(2005 * ms), EndUnixNanos: at(2010 * ms), Steps: steps(stepAt{2005 * ms, 2010 * ms, 200, "", 0, ""})}},
		// Due in the window but never sent before the deadline.
		{Picked: 0, Result: &pb.OperationResult{Index: 5, Operation: 1, ScheduledUnixNanos: at(1995 * ms),
			EndUnixNanos: at(7000 * ms), Error: pb.ErrorKind_ERROR_KIND_CANCELLED}},
		// A transport failure with a message.
		{Picked: at(1500 * ms), Result: &pb.OperationResult{Index: 6, Operation: 0, ScheduledUnixNanos: at(1500 * ms),
			StartUnixNanos: at(1500 * ms), EndUnixNanos: at(1501 * ms), Error: pb.ErrorKind_ERROR_KIND_TRANSPORT,
			Steps: steps(stepAt{1500 * ms, 1501 * ms, 0, nil, pb.ErrorKind_ERROR_KIND_TRANSPORT, "connection refused"})}},
	}
}

func summaryOf(t *testing.T, records []summary.Record) *pb.PhaseSummary {
	t.Helper()
	agg := summary.New(window, names)
	for _, r := range records {
		agg.Add(r)
	}
	s, err := agg.Summary(summary.Phase{Model: pb.LoadModel_LOAD_MODEL_OPEN, TargetRate: 7, MinSustained: 0.95, CPUFraction: 0.125, ResultsSpilled: 2,
		Generator: &pb.GeneratorStats{Read: 9, Rate: 1234.5, ReadySecs: 0.75, Buffer: 8192}})
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func selftestResult(t *testing.T) *pb.SelftestResult {
	t.Helper()
	agg := summary.New(window, names)
	for i := range int64(100) {
		scheduled := window.Start + i*ms
		agg.Add(summary.Record{Picked: scheduled, Result: &pb.OperationResult{ScheduledUnixNanos: scheduled, StartUnixNanos: scheduled, EndUnixNanos: scheduled + (50+i)*1000}})
	}
	s, err := agg.Summary(summary.Phase{Model: pb.LoadModel_LOAD_MODEL_OPEN})
	if err != nil {
		t.Fatal(err)
	}
	return &pb.SelftestResult{MaxRate: 40000, AddedLatency: s.GetLatency(), LoadgenCpuFraction: 0.31, Cpus: 6, AddedLatencyRate: 20000}
}

func capabilities() *pb.Capabilities {
	c := version.Capabilities()
	c.LoadgenVersion = "fixture"
	return c
}

func encode(t *testing.T, format files.Format, header *pb.FileHeader, messages ...proto.Message) []byte {
	t.Helper()
	var out bytes.Buffer
	w, err := files.NewWriter(&out, format, header)
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range messages {
		if err := w.Write(m); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Flush(); err != nil {
		t.Fatal(err)
	}
	return out.Bytes()
}

var formats = map[string]files.Format{"binpb": files.Binpb, "json": files.JSON}

// generate returns every fixture's bytes and the manifest.
func generate(t *testing.T) (map[string][]byte, Manifest) {
	t.Helper()
	out := map[string][]byte{}
	manifest := Manifest{About: "Conformance fixtures of proto/vibesys/evaluator/v1: every file kind in both formats " +
		"(binpb: length-delimited; json: proto3 JSON lines), and files a reader must reject. Written by " +
		"`go test ./internal/conformance -update` in sdk/vibesys-evaluator/go; read by the Go and Python tests."}
	header := func(kind pb.FileKind) *pb.FileHeader { return files.Header(kind, "fixture") }
	var resultMessages []proto.Message
	for _, r := range results() {
		resultMessages = append(resultMessages, r.Result)
	}
	ops := requests()
	for _, name := range []string{"binpb", "json"} {
		format := formats[name]
		requestsMessages := []proto.Message{&pb.RequestsHeader{OperationNames: names}}
		for _, op := range ops {
			requestsMessages = append(requestsMessages, op)
		}
		add := func(file, kind, description string, data []byte, count int) {
			out[file] = data
			manifest.Entries = append(manifest.Entries, Entry{File: file, Kind: kind, Format: name, Description: description, Valid: true, Messages: count})
		}
		add("requests."+name, "requests", "A recorded request stream of five operations: a three-step login with a captured token used raw, as a JSON "+
			"string and as a JSON value; a query; a form; a raw PUT that captures a header and two operations on exclusive key k7.",
			encode(t, format, header(pb.FileKind_FILE_KIND_REQUESTS), requestsMessages...), len(requestsMessages))
		add("results."+name, "results", "Results in completion order: kept bodies, digests, a status error, a transport "+
			"error, an operation spilled past the window after an exclusive-key wait, and one never sent (start 0).",
			encode(t, format, header(pb.FileKind_FILE_KIND_RESULTS), resultMessages...), len(resultMessages))
		s := summaryOf(t, results())
		p50, _ := summary.Percentile(s.GetLatency(), 50)
		p99, _ := summary.Percentile(s.GetLatency(), 99)
		latency, _ := summary.Decode(s.GetLatency())
		out["summary."+name] = encode(t, format, header(pb.FileKind_FILE_KIND_SUMMARY), s)
		manifest.Entries = append(manifest.Entries, Entry{File: "summary." + name, Kind: "summary", Format: name, Valid: true, Messages: 1,
			Description:      "The summary of results." + name + " over the window [base+1s, base+2s), base = " + "1700000000 s; latency counts successful operations only.",
			LatencyP50Micros: p50, LatencyP99Micros: p99, LatencyCount: latency.TotalCount(), FromResults: "results." + name,
			Histograms: expect(t, s)})
		add("selftest."+name, "selftest", "A selftest result.", encode(t, format, header(pb.FileKind_FILE_KIND_SELFTEST), selftestResult(t)), 1)
		manifest.Entries[len(manifest.Entries)-1].Histograms = expect(t, selftestResult(t))
		add("capabilities."+name, "capabilities", "What `loadgen --capabilities` prints (version replaced by \"fixture\").",
			encode(t, format, header(pb.FileKind_FILE_KIND_CAPABILITIES), capabilities()), 1)
	}
	reject := func(file, kind, format, description, message string, data []byte) {
		out[file] = data
		manifest.Entries = append(manifest.Entries, Entry{File: file, Kind: kind, Format: format, Description: description, Error: message})
	}
	v2 := header(pb.FileKind_FILE_KIND_REQUESTS)
	v2.SchemaVersion = 2
	reject("invalid-schema-version.binpb", "requests", "binpb", "A schema version this reader does not implement.", "unsupported schema version",
		encode(t, files.Binpb, v2, &pb.RequestsHeader{OperationNames: names[:1]}, messages.Op(0, messages.Get("/"))))
	reject("invalid-unknown-ref.json", "requests", "json", "A ref to a name no earlier step captures.", "names no capture of an earlier step",
		encode(t, files.JSON, header(pb.FileKind_FILE_KIND_REQUESTS), &pb.RequestsHeader{OperationNames: names[:1]},
			messages.Op(0, messages.Get("/kv/", messages.Raw("token")))))
	reject("invalid-operation-name.json", "requests", "json", "An operation name outside the pattern.", "operation_names",
		encode(t, files.JSON, header(pb.FileKind_FILE_KIND_REQUESTS), &pb.RequestsHeader{OperationNames: []string{"Login"}}, messages.Op(0, messages.Get("/"))))
	reject("invalid-kind.binpb", "requests", "binpb", "A summary file where a requests file is expected.", "FileHeader.kind",
		encode(t, files.Binpb, header(pb.FileKind_FILE_KIND_SUMMARY), summaryOf(t, results())))
	manifestData, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	out["manifest.json"] = append(manifestData, '\n')
	return out, manifest
}

func TestFixturesAreCurrent(t *testing.T) {
	generated, _ := generate(t)
	if *update {
		for name, data := range generated {
			if err := os.WriteFile(filepath.Join(dir, name), data, 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		// check_histograms.py is the hand-written Python check of these fixtures; rand.json is
		// the Python side's (rand_streams.py writes it, the Go SDK's test reads it);
		// __pycache__ is what running them leaves behind.
		name := entry.Name()
		ours := map[string]bool{"check_histograms.py": true, "rand_streams.py": true, "rand.json": true, "__pycache__": true}
		if _, ok := generated[name]; !ok && !ours[name] {
			t.Errorf("%s is not a generated fixture; remove it", name)
		}
	}
	for name, want := range generated {
		got, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil || !bytes.Equal(got, want) {
			t.Errorf("%s is stale; run go test ./internal/conformance -update", name)
		}
	}
}

func fresh(kind string) func() proto.Message {
	return map[string]func() proto.Message{
		"results":      func() proto.Message { return &pb.OperationResult{} },
		"summary":      func() proto.Message { return &pb.PhaseSummary{} },
		"selftest":     func() proto.Message { return &pb.SelftestResult{} },
		"capabilities": func() proto.Message { return &pb.Capabilities{} },
	}[kind]
}

var kinds = map[string]pb.FileKind{
	"requests": pb.FileKind_FILE_KIND_REQUESTS, "results": pb.FileKind_FILE_KIND_RESULTS, "summary": pb.FileKind_FILE_KIND_SUMMARY,
	"selftest": pb.FileKind_FILE_KIND_SELFTEST, "capabilities": pb.FileKind_FILE_KIND_CAPABILITIES,
}

func readMessages(t *testing.T, entry Entry) []proto.Message {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, entry.File))
	if err != nil {
		t.Fatal(err)
	}
	reader, _, err := files.NewReader(bytes.NewReader(data), formats[entry.Format], kinds[entry.Kind])
	if err != nil {
		t.Fatalf("%s: %v", entry.File, err)
	}
	var messages []proto.Message
	for {
		var message proto.Message = &pb.RequestsHeader{}
		if entry.Kind != "requests" {
			message = fresh(entry.Kind)()
		} else if len(messages) > 0 {
			message = &pb.PlannedOperation{}
		}
		if err := reader.Read(message); errors.Is(err, io.EOF) {
			return messages
		} else if err != nil {
			t.Fatalf("%s: %v", entry.File, err)
		}
		if err := protovalidate.Validate(message); err != nil {
			t.Fatalf("%s message %d: %v", entry.File, len(messages), err)
		}
		messages = append(messages, message)
	}
}

// Every fixture reads as the manifest says: valid ones decode, validate, and are the same
// messages in both formats; invalid ones are rejected with the stated error.
func TestFixturesReadAsTheManifestSays(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(dir, "manifest.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest Manifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		t.Fatal(err)
	}
	byFile := map[string][]proto.Message{}
	for _, entry := range manifest.Entries {
		if !entry.Valid {
			file, err := os.Open(filepath.Join(dir, entry.File))
			if err != nil {
				t.Fatal(err)
			}
			_, err = plan.Read(file, formats[entry.Format])
			_ = file.Close()
			if !errors.Is(err, plan.ErrInvalid) || !strings.Contains(err.Error(), entry.Error) {
				t.Errorf("%s: want an error containing %q, got %v", entry.File, entry.Error, err)
			}
			continue
		}
		messages := readMessages(t, entry)
		if len(messages) != entry.Messages {
			t.Fatalf("%s: %d messages, manifest says %d", entry.File, len(messages), entry.Messages)
		}
		byFile[entry.File] = messages
		if entry.Kind == "requests" {
			file, _ := os.Open(filepath.Join(dir, entry.File))
			if _, err := plan.Read(file, formats[entry.Format]); err != nil {
				t.Fatalf("%s: %v", entry.File, err)
			}
			_ = file.Close()
		}
		if len(entry.Histograms) > 0 {
			got := expect(t, messages[0])
			if len(got) != len(entry.Histograms) {
				t.Fatalf("%s: histograms %v, manifest %v", entry.File, got, entry.Histograms)
			}
			for name, want := range entry.Histograms {
				if got[name] != want {
					t.Fatalf("%s %s: decoded %+v, manifest %+v", entry.File, name, got[name], want)
				}
			}
		}
		if entry.Kind == "summary" {
			s := messages[0].(*pb.PhaseSummary)
			p50, _ := summary.Percentile(s.GetLatency(), 50)
			p99, _ := summary.Percentile(s.GetLatency(), 99)
			if p50 != entry.LatencyP50Micros || p99 != entry.LatencyP99Micros {
				t.Fatalf("%s: p50 %d p99 %d", entry.File, p50, p99)
			}
		}
	}
	for file, messages := range byFile {
		if !strings.HasSuffix(file, ".binpb") {
			continue
		}
		other := byFile[strings.TrimSuffix(file, ".binpb")+".json"]
		for i := range messages {
			if !proto.Equal(messages[i], other[i]) {
				t.Fatalf("%s message %d differs between binpb and json", file, i)
			}
		}
	}
}
