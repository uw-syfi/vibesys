package microservices

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/check"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// The test binary is also the task script: run with a command of Main (benchmark, phase,
// generate, seed), it is Main of testTask, configured by these variables.
const (
	targetEnv  = "MS_TEST_TARGET"
	captureEnv = "MS_TEST_CAPTURE"
	openEnv    = "MS_TEST_OPEN_RATE"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && !strings.HasPrefix(os.Args[1], "-test.") {
		Main(testTask())
	}
	dir, err := os.MkdirTemp("", "ms-test-")
	if err != nil {
		panic(err)
	}
	loadgen := filepath.Join(dir, "loadgen")
	build := exec.Command(filepath.Join(runtime.GOROOT(), "bin", "go"), "build", "-o", loadgen, "../cmd/loadgen")
	build.Stdout, build.Stderr = os.Stderr, os.Stderr
	if err := build.Run(); err != nil {
		panic(fmt.Sprintf("building loadgen: %v", err))
	}
	os.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

type kvData struct {
	Keys int `json:"keys"`
}

type kvModel struct {
	keys   int
	phase  int
	writes int
}

type kvInfo struct {
	Key   int `json:"key"`
	Value int `json:"value,omitempty"`
}

func kvWorkload() workload.Workload[kvData, *kvModel] {
	return workload.Workload[kvData, *kvModel]{
		Model: func(data kvData, _ *workload.Rand) (*kvModel, error) { return &kvModel{keys: data.Keys}, nil },
		BeginPhase: func(m *kvModel, phase int) error {
			m.phase = phase
			return nil
		},
		Operations: []workload.Operation[*kvModel]{
			{Name: "get", Weight: 3, Build: func(m *kvModel, r *workload.Rand) workload.Request {
				key := r.Below(m.keys)
				return workload.Request{Steps: []workload.Step{workload.Get(fmt.Sprintf("/kv/%d", key))}, Info: kvInfo{Key: key}}
			}},
			{Name: "put", Weight: 1, Build: func(m *kvModel, r *workload.Rand) workload.Request {
				key := r.Below(m.keys)
				m.writes++
				value := 1_000_000*(m.phase+1) + m.writes
				return workload.Request{
					Steps: []workload.Step{workload.HTTP("PUT", fmt.Sprintf("/kv/%d", key), workload.Body([]byte(fmt.Sprint(value))))},
					Info:  kvInfo{Key: key, Value: value},
				}
			}},
		},
	}
}

func testTask() Task[kvData, *kvModel] {
	w := kvWorkload()
	capture := os.Getenv(captureEnv) != ""
	return Task[kvData, *kvModel]{
		Metrics: map[string]evaluator.Metric{
			"ops_per_sec": {Unit: "ops/s", Direction: evaluator.Max},
			"p99_ms":      {Unit: "ms", Direction: evaluator.Min},
		},
		Dataset: func() (kvData, error) { return kvData{Keys: 8}, nil },
		System:  Local(os.Getenv(targetEnv)),
		Seed: func(ctx context.Context, data kvData) error {
			for key := range data.Keys {
				req, err := http.NewRequestWithContext(ctx, http.MethodPut,
					fmt.Sprintf("%s/kv/%d", os.Getenv(targetEnv), key), strings.NewReader("0"))
				if err != nil {
					return err
				}
				resp, err := http.DefaultClient.Do(req)
				if err != nil {
					return err
				}
				resp.Body.Close()
			}
			return nil
		},
		Workload: &w,
		Capture:  capture,
		Benchmark: func(ctx context.Context, run *Run) error {
			closed, err := run.ClosedLoop(ctx, Closed{Clients: 16, Secs: time.Second, Warmup: 200 * time.Millisecond})
			if err != nil {
				return err
			}
			if capture {
				if err := run.Fault(ctx, "pause", "kv", func(context.Context) error {
					time.Sleep(20 * time.Millisecond)
					return nil
				}); err != nil {
					return err
				}
			}
			rate := 500.0
			if text := os.Getenv(openEnv); text != "" {
				fmt.Sscan(text, &rate)
			}
			open, err := run.OpenLoop(ctx, Open{Rate: rate, Secs: time.Second, Warmup: 200 * time.Millisecond, Arrivals: Uniform})
			if err != nil {
				return err
			}
			return run.Report(map[string]float64{"ops_per_sec": closed.OpsPerSec, "p99_ms": open.P99Ms})
		},
	}
}

// serverDelay is how long the test application takes per request: with latencies this long, the
// open loop's send lag on a busy machine stays far below the guard's 10% of the latency.
const serverDelay = 50 * time.Millisecond

// kvServer is the application: GET and PUT of /kv/<key>; every response fails with status
// when failing is set.
func kvServer(t *testing.T, failing int) *httptest.Server {
	var mu sync.Mutex
	values := map[string]string{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(serverDelay)
		if failing != 0 {
			w.WriteHeader(failing)
			return
		}
		key := strings.TrimPrefix(r.URL.Path, "/kv/")
		mu.Lock()
		defer mu.Unlock()
		switch r.Method {
		case http.MethodPut:
			body, _ := io.ReadAll(r.Body)
			values[key] = string(body)
			fmt.Fprint(w, "ok")
		case http.MethodGet:
			fmt.Fprintf(w, `{"value":%q}`, values[key])
		default:
			w.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

// benchmark runs the test binary's benchmark command and returns the artifacts directory, the
// records and the exit code.
func benchmark(t *testing.T, target string, env ...string) (string, []map[string]any, int) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	artifacts := filepath.Join(dir, "artifacts")
	output := filepath.Join(dir, "records.jsonl")
	cmd := exec.Command(exe, "benchmark", "--project-root", dir, "--vs-output", output, "--seed", "7")
	cmd.Env = append(os.Environ(), append([]string{targetEnv + "=" + target, evaluator.ArtifactsEnv + "=" + artifacts}, env...)...)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	err = cmd.Run()
	code := 0
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		code = exit.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	t.Logf("benchmark stderr:\n%s", stderr.String())
	file, err := os.Open(output)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	var records []map[string]any
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		record := map[string]any{}
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			t.Fatal(err)
		}
		records = append(records, record)
	}
	return artifacts, records, code
}

func TestBenchmarkRunsPhasesAndReports(t *testing.T) {
	server := kvServer(t, 0)
	artifacts, records, code := benchmark(t, server.URL)
	if code != 0 {
		t.Fatalf("exit %d, records %v", code, records)
	}
	last := records[len(records)-1]
	if last["kind"] != "result" {
		t.Fatalf("the last record is %v", last)
	}
	values := last["values"].(map[string]any)
	if values["ops_per_sec"].(float64) <= 0 || values["p99_ms"].(float64) <= 0 {
		t.Fatalf("values %v", values)
	}
	for _, name := range []string{"seeds.json", "dataset.json", "seed.log",
		"phase-0/output.binpb", "phase-0/summary.binpb", "phase-0/results.binpb.gz", "phase-0/phase.log",
		"phase-1/output.binpb"} {
		if _, err := os.Stat(filepath.Join(artifacts, name)); err != nil {
			t.Errorf("artifacts: %v", err)
		}
	}
	for _, name := range []string{"history.binpb", "phase-0/kept.binpb", "phase-0/results.binpb", "phase-0/input.binpb"} {
		if _, err := os.Stat(filepath.Join(artifacts, name)); err == nil {
			t.Errorf("a benchmark without capture left %s", name)
		}
	}
	output, err := phase.ReadOutput(filepath.Join(artifacts, "phase-1"))
	if err != nil {
		t.Fatal(err)
	}
	if s := output.GetSummary(); s.GetModel() != pb.LoadModel_LOAD_MODEL_OPEN || s.GetTargetRate() != 500 {
		t.Errorf("phase 1's summary: %v", s)
	}
}

func TestCaptureRecordsAHistoryTheCheckerReads(t *testing.T) {
	server := kvServer(t, 0)
	artifacts, records, code := benchmark(t, server.URL, captureEnv+"=1")
	if code != 0 {
		t.Fatalf("exit %d, records %v", code, records)
	}
	raw, err := os.ReadFile(filepath.Join(artifacts, historyFile))
	if err != nil {
		t.Fatal(err)
	}
	manifest := &pb.HistoryManifest{}
	if err := proto.Unmarshal(raw, manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.GetPhases() != 2 || len(manifest.GetFaults()) != 1 || manifest.GetDataset() != phase.DatasetFile {
		t.Fatalf("manifest %v", manifest)
	}
	history, err := check.OpenHistory(artifacts)
	if err != nil {
		t.Fatal(err)
	}
	fault := history.Faults[0]
	if fault.Kind != "pause" || fault.AfterPhase != 0 || fault.End-fault.Start < int64(20*time.Millisecond) {
		t.Errorf("fault %+v", fault)
	}
	for i := range history.Phases {
		calls, err := history.Phase(i)
		if err != nil {
			t.Fatal(err)
		}
		if len(calls) == 0 {
			t.Fatalf("phase %d has no calls", i)
		}
		gets := 0
		for _, call := range calls {
			if call.Client < 0 {
				t.Fatalf("phase %d: call %d has no client", i, call.Index)
			}
			var info kvInfo
			if err := json.Unmarshal(call.Info, &info); err != nil {
				t.Fatalf("phase %d: call %d info %s: %v", i, call.Index, call.Info, err)
			}
			if call.Op == "put" && info.Value/1_000_000 != i+1 {
				t.Fatalf("phase %d: put %d wrote %d: BeginPhase did not see the phase", i, call.Index, info.Value)
			}
			if call.Op == "get" && !call.Lost() {
				gets++
				if !strings.Contains(string(call.Steps[0].Body), `"value"`) {
					t.Fatalf("phase %d: get %d kept body %q", i, call.Index, call.Steps[0].Body)
				}
			}
			if i == 1 && call.Start < fault.End {
				t.Fatalf("phase 1's call %d started before the fault ended", call.Index)
			}
			if i == 0 && call.End > fault.Start {
				t.Fatalf("phase 0's call %d ended after the fault started", call.Index)
			}
		}
		if gets == 0 {
			t.Fatalf("phase %d kept no get", i)
		}
	}
}

func TestAPhaseThatIsNotAMeasurementEndsTheSample(t *testing.T) {
	server := kvServer(t, http.StatusInternalServerError)
	_, records, code := benchmark(t, server.URL)
	if code == 0 {
		t.Fatal("a sample of failing phases exited 0")
	}
	last := records[len(records)-1]
	message, _ := last["message"].(string)
	if last["kind"] != "error" || !strings.Contains(message, "phase 0 (closed loop, 16 clients, 1 s after 0.2 s warmup): error rate") {
		t.Fatalf("the last record is %v", last)
	}
}

func TestAnUnsustainedOpenLoopCarriesThePartial(t *testing.T) {
	server := kvServer(t, 0)
	_, records, code := benchmark(t, server.URL, openEnv+"=1e6")
	if code == 0 {
		t.Fatal("an open loop far past capacity exited 0")
	}
	last := records[len(records)-1]
	message, _ := last["message"].(string)
	if last["kind"] != "error" || !strings.HasPrefix(message, "phase 1 (open loop at 1000000.0 ops/s (uniform)") {
		t.Fatalf("the last record is %v", last)
	}
	partial, _ := last["partial"].(map[string]any)
	if partial["name"] != "last_phase_ops_per_sec" {
		t.Fatalf("the error record's partial is %v", last["partial"])
	}
}

func TestLoadsAreValidated(t *testing.T) {
	for _, load := range []Load{
		Closed{Clients: 0, Secs: time.Second},
		Closed{Clients: 1},
		Closed{Clients: 1, Secs: time.Second, Warmup: -time.Second},
		Open{Rate: 0, Secs: time.Second},
		Open{Rate: 10, Secs: time.Second, Arrivals: "bursty"},
	} {
		if _, err := load.input(); err == nil {
			t.Errorf("%+v was accepted", load)
		}
	}
	in, err := Open{Rate: 10, Secs: 2 * time.Second, Warmup: time.Second}.input()
	if err != nil || in.GetArrivals() != pb.Arrivals_ARRIVALS_POISSON || in.GetSecs() != 2 || in.GetWarmupSecs() != 1 {
		t.Fatalf("%v, %v", in, err)
	}
}

func TestAFaultGoesBetweenPhases(t *testing.T) {
	p := &phases{log: func(string) {}, now: time.Now, artifacts: t.TempDir()}
	ran := false
	action := func(context.Context) error { ran = true; return nil }
	if err := p.fault(context.Background(), "restart", "kv", action); err == nil || ran {
		t.Fatalf("a fault before the first phase ran: %v", err)
	}
	p.next = 1
	if err := p.fault(context.Background(), "Restart!", "kv", action); err == nil || ran {
		t.Fatalf("a fault of a bad kind ran: %v", err)
	}
	failing := func(context.Context) error { return errors.New("no such pod") }
	if err := p.fault(context.Background(), "restart", "kv", failing); err == nil || !strings.Contains(err.Error(), "no such pod") {
		t.Fatalf("a failed action: %v", err)
	}
	if len(p.faults) != 1 || p.faults[0].GetAfterPhase() != 0 {
		t.Fatalf("the failed action was not recorded: %v", p.faults)
	}
}
