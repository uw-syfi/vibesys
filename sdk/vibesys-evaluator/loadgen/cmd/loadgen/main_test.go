package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/messages"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/version"
)

func invoke(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute) // hang guard only
	defer cancel()
	code := run(ctx, args, &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

func formatName(format files.Format) string {
	if format == files.JSON {
		return "json"
	}
	return "binpb"
}

// readFile reads a file of one kind: its messages after the header, each validated.
func readFile[M proto.Message](t *testing.T, data []byte, format files.Format, kind pb.FileKind, fresh func() M) []M {
	t.Helper()
	reader, header, err := files.NewReader(bytes.NewReader(data), format, kind)
	if err != nil {
		t.Fatal(err)
	}
	if err := protovalidate.Validate(header); err != nil {
		t.Fatal(err)
	}
	var messages []M
	for {
		message := fresh()
		if err := reader.Read(message); errors.Is(err, io.EOF) {
			return messages
		} else if err != nil {
			t.Fatal(err)
		}
		if err := protovalidate.Validate(message); err != nil {
			t.Fatalf("%v: %v", message, err)
		}
		messages = append(messages, message)
	}
}

func TestCapabilitiesAreValidInBothFormats(t *testing.T) {
	for _, format := range []files.Format{files.Binpb, files.JSON} {
		code, stdout, stderr := invoke(t, "--capabilities", "--format", formatName(format))
		if code != exitOK {
			t.Fatalf("exit %d: %s", code, stderr)
		}
		got := readFile(t, []byte(stdout), format, pb.FileKind_FILE_KIND_CAPABILITIES, func() *pb.Capabilities { return &pb.Capabilities{} })
		if len(got) != 1 || !proto.Equal(got[0], version.Capabilities()) {
			t.Fatalf("capabilities %v", got)
		}
	}
}

func TestUsageErrorsExitTwo(t *testing.T) {
	for _, args := range [][]string{
		{},
		{"bogus"},
		{"closed", "--target", "http://localhost:1", "--secs", "1", "--warmup", "0", "--requests", "x", "--results", "y"},
		{"closed", "--clients", "0", "--target", "http://localhost:1", "--secs", "1", "--warmup", "0", "--requests", "x", "--results", "y", "--summary", "z"},
		{"open", "--rate", "10", "--arrivals", "bursty", "--target", "http://localhost:1", "--secs", "1", "--warmup", "0", "--requests", "x", "--results", "y", "--summary", "z"},
		{"--capabilities", "--format", "yaml"},
		{"probe", "--target", "ftp://host-a", "--requests", "x", "--rounds", "1", "--deadline", "1"},
	} {
		if code, _, stderr := invoke(t, args...); code != exitUsage || !strings.Contains(stderr, "usage:") {
			t.Errorf("%v: exit %d, %s", args, code, stderr)
		}
	}
}

type phaseFiles struct{ requests, results, summary string }

func writeRequests(t *testing.T, data []byte) phaseFiles {
	t.Helper()
	dir := t.TempDir()
	f := phaseFiles{filepath.Join(dir, "requests"), filepath.Join(dir, "results"), filepath.Join(dir, "summary")}
	if err := os.WriteFile(f.requests, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return f
}

// A plan it cannot run exits 2 before sending anything, and leaves no summary.
func TestUnrunnablePlansExitTwoWithoutASummary(t *testing.T) {
	var hits atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { hits.Add(1) }))
	defer server.Close()
	names := []string{"op"}
	for name, data := range map[string][]byte{
		"schema version 2": messages.RequestsFileVersion(files.Binpb, 2, names, messages.Op(0, messages.Get("/"))),
		"unknown ref":      messages.RequestsFile(files.Binpb, names, messages.Op(0, messages.Get("/", messages.Raw("nope")))),
		"not a plan":       []byte("garbage"),
	} {
		f := writeRequests(t, data)
		code, _, stderr := invoke(t, "closed", "--target", server.URL, "--clients", "1", "--secs", "1", "--warmup", "0",
			"--requests", f.requests, "--results", f.results, "--summary", f.summary)
		if code != exitUsage {
			t.Errorf("%s: exit %d: %s", name, code, stderr)
		}
		if _, err := os.Stat(f.summary); !os.IsNotExist(err) {
			t.Errorf("%s: a summary was written", name)
		}
	}
	if hits.Load() != 0 {
		t.Fatalf("%d requests reached the target", hits.Load())
	}
}

// End to end: a multi-step plan with a captured token, closed loop, whose plan runs out: exit 0,
// the summary says exhausted, and every result has both steps.
func TestClosedPhaseEndToEndReportsExhaustion(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /login", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte(`{"token":"t1"}`)) })
	mux.HandleFunc("GET /me", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer t1" {
			w.WriteHeader(http.StatusUnauthorized)
		}
	})
	server := httptest.NewServer(mux)
	defer server.Close()
	login := messages.CaptureJSON(messages.Post(messages.Text(`{}`), "/login"), "token", "/token")
	me := messages.Header(messages.Get("/me"), "Authorization", "Bearer ", messages.Raw("token"))
	var ops []*pb.PlannedOperation
	for range 50 {
		ops = append(ops, messages.Op(0, login, me))
	}
	for _, format := range []files.Format{files.Binpb, files.JSON} {
		f := writeRequests(t, messages.RequestsFile(format, []string{"login_me"}, ops...))
		code, _, stderr := invoke(t, "closed", "--target", server.URL, "--clients", "4", "--secs", "1", "--warmup", "0",
			"--requests", f.requests, "--results", f.results, "--summary", f.summary, "--format", formatName(format))
		if code != exitOK {
			t.Fatalf("exit %d: %s", code, stderr)
		}
		summaryData, _ := os.ReadFile(f.summary)
		summaries := readFile(t, summaryData, format, pb.FileKind_FILE_KIND_SUMMARY, func() *pb.PhaseSummary { return &pb.PhaseSummary{} })
		if len(summaries) != 1 || !summaries[0].GetExhausted() || summaries[0].GetErrors() != 0 || summaries[0].GetCompleted() != 50 {
			t.Fatalf("summary %v", summaries)
		}
		resultsData, _ := os.ReadFile(f.results)
		results := readFile(t, resultsData, format, pb.FileKind_FILE_KIND_RESULTS, func() *pb.OperationResult { return &pb.OperationResult{} })
		if len(results) != 50 {
			t.Fatalf("%d results", len(results))
		}
		for _, r := range results {
			if len(r.GetSteps()) != 2 || r.GetSteps()[1].GetStatus() != 200 || r.GetSteps()[1].GetDigest() == nil {
				t.Fatalf("result %v", r)
			}
		}
	}
}

func TestOpenPhaseEndToEnd(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	var ops []*pb.PlannedOperation
	for range 400 {
		ops = append(ops, messages.Op(0, messages.Get("/")))
	}
	f := writeRequests(t, messages.RequestsFile(files.Binpb, []string{"get"}, ops...))
	code, _, stderr := invoke(t, "open", "--target", server.URL, "--rate", "200", "--seed", "3", "--secs", "1", "--warmup", "0.5",
		"--min-sustained", "0.5", "--requests", f.requests, "--results", f.results, "--summary", f.summary)
	if code != exitOK {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	data, _ := os.ReadFile(f.summary)
	s := readFile(t, data, files.Binpb, pb.FileKind_FILE_KIND_SUMMARY, func() *pb.PhaseSummary { return &pb.PhaseSummary{} })[0]
	if s.GetModel() != pb.LoadModel_LOAD_MODEL_OPEN || s.GetTargetRate() != 200 || !s.GetSustained() || s.GetExhausted() || len(s.GetOperations()) != 1 {
		t.Fatalf("summary %v", s)
	}
}

func TestResultsThatCannotBeWrittenExitOne(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer server.Close()
	f := writeRequests(t, messages.RequestsFile(files.Binpb, []string{"get"}, messages.Op(0, messages.Get("/"))))
	code, _, _ := invoke(t, "closed", "--target", server.URL, "--clients", "1", "--secs", "0.1", "--warmup", "0",
		"--requests", f.requests, "--results", filepath.Join(f.results, "missing", "dir"), "--summary", f.summary)
	if code != exitFailure {
		t.Fatalf("exit %d", code)
	}
}

// Probe succeeds once the target answers every operation twice, on fresh connections.
func TestProbeWaitsForReadinessAndFailsAtTheDeadline(t *testing.T) {
	var calls atomic.Int64
	ready := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if calls.Add(1) <= 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	}))
	defer ready.Close()
	f := writeRequests(t, messages.RequestsFile(files.Binpb, []string{"health"}, messages.Op(0, messages.Get("/health"))))
	if code, _, stderr := invoke(t, "probe", "--target", ready.URL, "--requests", f.requests, "--rounds", "2", "--deadline", "30"); code != exitOK {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	if calls.Load() != 5 {
		t.Fatalf("%d calls, want 3 failures then 2 successes", calls.Load())
	}
	never := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusBadGateway) }))
	defer never.Close()
	code, _, stderr := invoke(t, "probe", "--target", never.URL, "--requests", f.requests, "--rounds", "1", "--deadline", "0.3")
	if code != exitFailure || !strings.Contains(stderr, "operation 0 (health)") || !strings.Contains(stderr, "HTTP 502") {
		t.Fatalf("exit %d: %s", code, stderr)
	}
}

func TestSelftestWritesAValidResult(t *testing.T) {
	path := filepath.Join(t.TempDir(), "selftest")
	code, _, stderr := invoke(t, "selftest", "--summary", path, "--step-secs", "0.3", "--max-rate", "1000", "--format", "json")
	if code != exitOK {
		t.Fatalf("exit %d: %s", code, stderr)
	}
	data, _ := os.ReadFile(path)
	got := readFile(t, data, files.JSON, pb.FileKind_FILE_KIND_SELFTEST, func() *pb.SelftestResult { return &pb.SelftestResult{} })
	// What rate this host sustains is not the test's business; the search and the file are.
	if len(got) != 1 || got[0].GetMaxRate() > 1000 || got[0].GetAddedLatencyRate() != got[0].GetMaxRate()/2 {
		t.Fatalf("selftest %v", got)
	}
}
