package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
)

// Run with fakeLoadgenEnv set, the test binary is a loadgen that writes a summary, the results
// and the generator's kept file, and its argv into the summary's directory.
const (
	fakeLoadgenEnv  = "PHASE_TEST_FAKE_LOADGEN"
	fakeLoadgenFail = "PHASE_TEST_FAKE_LOADGEN_FAIL"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 {
		if role, ok := accuracyRoles[os.Args[1]]; ok {
			os.Exit(role(os.Args[2:]))
		}
	}
	if os.Getenv(fakeLoadgenEnv) != "" {
		os.Exit(fakeLoadgen(os.Args[1:]))
	}
	os.Exit(m.Run())
}

func fakeLoadgen(args []string) int {
	if os.Getenv(fakeLoadgenFail) != "" {
		os.Stderr.WriteString("loadgen: the target refused every connection\n")
		return 1
	}
	flag := func(name string) string {
		i := slices.Index(args, name)
		return args[i+1]
	}
	summary := flag("--summary")
	_ = os.WriteFile(filepath.Join(filepath.Dir(summary), "argv.json"), must(json.Marshal(args)), 0o644)
	_ = os.WriteFile(flag("--results"), []byte("results"), 0o644)
	_ = os.WriteFile(filepath.Join(flag("--state"), phase.KeptFile), []byte("kept"), 0o644)
	out, err := os.Create(summary)
	if err != nil {
		return 1
	}
	defer out.Close()
	w, err := files.NewWriter(out, files.Binpb, files.Header(pb.FileKind_FILE_KIND_SUMMARY, "fake"))
	if err != nil {
		return 1
	}
	if err := w.Write(fakeSummary(0)); err != nil || w.Flush() != nil {
		return 1
	}
	return 0
}

func must(b []byte, err error) []byte {
	if err != nil {
		panic(err)
	}
	return b
}

func fakeSummary(errors uint64) *pb.PhaseSummary {
	return &pb.PhaseSummary{
		Model: pb.LoadModel_LOAD_MODEL_CLOSED, Sent: 100, Completed: 100, Errors: errors, AchievedRate: 50,
		OperationNames: []string{"get"}, Operations: []*pb.OperationSummary{{Operation: 0, Completed: 100}},
	}
}

func podRequest(t *testing.T, capture bool) (string, string) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	work := t.TempDir()
	in := &pb.PhaseInput{SampleSeed: 7, PhaseIndex: 2, Model: pb.LoadModel_LOAD_MODEL_CLOSED, Clients: 4, Secs: 1, Capture: capture}
	if err := os.WriteFile(filepath.Join(work, phase.InputFile), must(proto.Marshal(in)), 0o644); err != nil {
		t.Fatal(err)
	}
	req := map[string]any{"work_dir": work, "target": "http://kv:8080", "generator": []string{"/bin/generator", "--fast"},
		"dataset_file": "/data/dataset.json", "min_sustained_ratio": 0.95, "loadgen": exe}
	return work, string(must(json.Marshal(req)))
}

func TestAPhasePodStreamsEventsThenItsResponse(t *testing.T) {
	t.Setenv(fakeLoadgenEnv, "1")
	for _, capture := range []bool{false, true} {
		work, req := podRequest(t, capture)
		var stdout, stderr bytes.Buffer
		if code := run([]string{"phase", "pod"}, strings.NewReader(req), &stdout, &stderr); code != 0 {
			t.Fatalf("capture %v: exit %d: %s %s", capture, code, stdout.String(), stderr.String())
		}
		lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
		var stages []string
		for _, line := range lines[:len(lines)-1] {
			var event struct {
				Event struct {
					Stage, Message string
					UnixNanos      string `json:"unix_nanos"`
				}
			}
			if err := json.Unmarshal([]byte(line), &event); err != nil || event.Event.UnixNanos == "" {
				t.Fatalf("event line %q: %v", line, err)
			}
			stages = append(stages, event.Event.Stage)
		}
		if !slices.Equal(stages, []string{"loadgen", "files", "done"}) {
			t.Errorf("stages %v", stages)
		}
		var resp map[string]any
		if err := json.Unmarshal([]byte(lines[len(lines)-1]), &resp); err != nil {
			t.Fatal(err)
		}
		if resp["completed"] != "100" || resp["achieved_rate"] != 50.0 {
			t.Errorf("response %v", resp)
		}
		var argv []string
		if err := json.Unmarshal(must(os.ReadFile(filepath.Join(work, "argv.json"))), &argv); err != nil {
			t.Fatal(err)
		}
		generator := argv[slices.Index(argv, "--")+1:]
		if generator[0] != "/bin/generator" || generator[1] != "--fast" || generator[2] != "generate" ||
			!slices.Contains(generator, "/data/dataset.json") {
			t.Errorf("generator command %v", generator)
		}
		if slices.Contains(argv, "--record-clients") != capture {
			t.Errorf("capture %v: argv %v", capture, argv)
		}
		want := []string{phase.OutputFile, phase.SummaryFile, phase.PackedResultsFile}
		if capture {
			want = []string{phase.OutputFile, phase.SummaryFile, phase.ResultsFile, phase.KeptFile}
		}
		for _, name := range want {
			if _, err := os.Stat(filepath.Join(work, name)); err != nil {
				t.Errorf("capture %v: %v", capture, err)
			}
		}
	}
}

func TestAPhasePodWhoseLoadgenFailsIsADomainError(t *testing.T) {
	t.Setenv(fakeLoadgenEnv, "1")
	t.Setenv(fakeLoadgenFail, "1")
	_, req := podRequest(t, false)
	var stdout, stderr bytes.Buffer
	code := run([]string{"phase", "pod"}, strings.NewReader(req), &stdout, &stderr)
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	var out map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &out); err != nil {
		t.Fatal(err)
	}
	if code != 1 || out["error_kind"] != "phase" || !strings.Contains(out["message"].(string), "refused every connection") {
		t.Fatalf("exit %d, %v", code, out)
	}
}

func TestAPhaseIsJudged(t *testing.T) {
	dir := t.TempDir()
	write := func(errors uint64) {
		output := &pb.PhaseOutput{Summary: fakeSummary(errors), LoadgenSecs: 3}
		if err := os.WriteFile(filepath.Join(dir, phase.OutputFile), must(proto.Marshal(output)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	judge := func(phaseDir, validity string) (int, map[string]any) {
		return call(t, "phase", "judge", `{"phase_dir":`+jsonQuote(phaseDir)+`,
			"input":{"phase_index":1,"model":"LOAD_MODEL_CLOSED","clients":4,"secs":1,"warmup_secs":0.5},
			"validity":`+validity+`}`)
	}
	valid := `{"max_error_rate":0,"min_sustained_ratio":0.95,"min_ops_per_operation":1}`
	write(0)
	code, out := judge(dir, valid)
	if code != 0 || out["failure"] != "" || out["measurement"].(map[string]any)["ops_per_sec"] != 50.0 {
		t.Fatalf("exit %d, %v", code, out)
	}
	write(5)
	_, out = judge(dir, valid)
	if !strings.HasPrefix(out["failure"].(string), "phase 1 (closed loop, 4 clients, 1 s after 0.5 s warmup): error rate 5.0000%") ||
		out["measurement"] != nil {
		t.Fatalf("%v", out)
	}
	_, out = judge(filepath.Join(dir, "missing"), valid)
	if !strings.Contains(out["failure"].(string), "left no usable output") {
		t.Fatalf("%v", out)
	}
	code, out = judge(dir, `{"max_error_rate":2,"min_sustained_ratio":0.95}`)
	if code != 1 || out["error_kind"] != "bad_request" {
		t.Fatalf("exit %d, %v", code, out)
	}
}
