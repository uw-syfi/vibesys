package microservices

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"iter"
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
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// The test binary is also the task script: run with a command of Main (benchmark, phase,
// generate, seed), it is Main of testTask, configured by these variables.
const (
	targetEnv   = "MS_TEST_TARGET"
	captureEnv  = "MS_TEST_CAPTURE"
	openEnv     = "MS_TEST_OPEN_RATE"
	accuracyEnv = "MS_TEST_ACCURACY" // the fault between the accuracy check's phases
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
		Accuracy: testAccuracy(os.Getenv(accuracyEnv)),
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

// testAccuracy is the accuracy check of the test task: two phases with fault between them, and
// kvOracle; nil without a fault.
func testAccuracy(fault string) *Accuracy {
	var f Fault
	switch fault {
	case "":
		return nil
	case "pause":
		f = FaultFunc(0, "pause", "kv", func(context.Context) error { time.Sleep(20 * time.Millisecond); return nil })
	case "wipe": // the application loses what it stored, as a store without durability would
		f = FaultFunc(0, "wipe", "kv", func(ctx context.Context) error {
			req, err := http.NewRequestWithContext(ctx, http.MethodPost, os.Getenv(targetEnv)+"/admin/wipe", nil)
			if err != nil {
				return err
			}
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				return err
			}
			return resp.Body.Close()
		})
	case "restart":
		f = Restart(0, "kv")
	}
	return &Accuracy{
		Phases: 2,
		Load:   Open{Rate: 100, Secs: time.Second, Warmup: 200 * time.Millisecond, Arrivals: Uniform},
		Faults: []Fault{f},
		Oracle: func(*check.History) ([]check.AnySpec, error) { return []check.AnySpec{kvSpec()}, nil },
	}
}

// kvSpec is the test application's oracle: a key reads the last value written to it (the seed's
// "0" before any), at once.
func kvSpec() *check.Spec[check.Register[string], kvOp, string] {
	return &check.Spec[check.Register[string], kvOp, string]{
		Name: "kv",
		Project: func(phases iter.Seq[[]check.Call], out *check.Projection[kvOp, string]) error {
			for calls := range phases {
				for i := range calls {
					c := &calls[i]
					var info kvInfo
					if err := json.Unmarshal(c.Info, &info); err != nil {
						return err
					}
					piece := check.Piece[kvOp, string]{Keys: []string{fmt.Sprint(info.Key)}, Origin: c, Unknown: c.Lost()}
					switch c.Op {
					case "put":
						piece.Write, piece.In = true, kvOp{write: true, value: fmt.Sprint(info.Value)}
					case "get":
						if !piece.Unknown {
							var body struct{ Value string }
							if err := json.Unmarshal(c.Steps[0].Body, &body); err != nil {
								out.Violation(c, "GET answered %q", c.Steps[0].Body)
								continue
							}
							piece.Out = body.Value
						}
					}
					out.Add(piece)
				}
			}
			return nil
		},
		EffectWindow: 10 * time.Second,
		Init:         func([]string) check.Register[string] { return check.NewRegister("0") },
		Step: func(r check.Register[string], o check.Op[kvOp, string]) ([]check.Register[string], string) {
			r = r.Prune(o.Floor, 0)
			if o.In.write {
				written := r.Write(o.In.value, o.Return)
				if o.Unknown {
					return []check.Register[string]{r, written}, ""
				}
				return []check.Register[string]{written}, ""
			}
			if o.Unknown || r.MayShow(o.Out, o.Call, 0) {
				return []check.Register[string]{r}, ""
			}
			return nil, fmt.Sprintf("read %q", o.Out)
		},
		Equal: func(a, b check.Register[string]) bool { return a.Equal(b) },
		Hash:  func(r check.Register[string]) uint64 { return r.Hash() },
	}
}

type kvOp struct {
	write bool
	value string
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
		mu.Lock()
		defer mu.Unlock()
		if r.URL.Path == "/admin/wipe" {
			clear(values)
			return
		}
		key := strings.TrimPrefix(r.URL.Path, "/kv/")
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
	raw, err := os.ReadFile(filepath.Join(artifacts, phase.HistoryFile))
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

// accuracyRun runs the test binary's accuracy command with fault and returns the report
// directory, the exit code and the standard error.
func accuracyRun(t *testing.T, target, fault string, args ...string) (string, int, string) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(t.TempDir(), "accuracy")
	cmd := exec.Command(exe, append([]string{"accuracy", "--out", out, "--seed", "7"}, args...)...)
	cmd.Env = append(os.Environ(), targetEnv+"="+target, accuracyEnv+"="+fault)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	err = cmd.Run()
	var exit *exec.ExitError
	code := 0
	if errors.As(err, &exit) {
		code = exit.ExitCode()
	} else if err != nil {
		t.Fatal(err)
	}
	t.Logf("accuracy stderr:\n%s", stderr.String())
	return out, code, stderr.String()
}

func TestAccuracyPassesAnApplicationThatKeepsItsWrites(t *testing.T) {
	server := kvServer(t, 0)
	out, code, _ := accuracyRun(t, server.URL, "pause")
	if code != 0 {
		t.Fatalf("exit %d", code)
	}
	history, err := check.OpenHistory(filepath.Join(out, "history"))
	if err != nil {
		t.Fatal(err)
	}
	if history.Phases != 2 || len(history.Faults) != 1 || history.Faults[0].Kind != "pause" || history.Dataset == "" {
		t.Fatalf("history: %d phases, faults %+v, dataset %q", history.Phases, history.Faults, history.Dataset)
	}
	if _, err := os.Stat(filepath.Join(out, "report.txt")); err != nil {
		t.Fatal(err)
	}
	// The history is judged again without a run.
	again := exec.Command(os.Args[0], "accuracy", "--history", filepath.Join(out, "history"), "--out", t.TempDir())
	again.Env = append(os.Environ(), accuracyEnv+"=pause")
	if err := again.Run(); err != nil {
		t.Fatalf("judging the history again: %v", err)
	}
}

func TestAccuracyFailsAnApplicationThatLosesItsWrites(t *testing.T) {
	server := kvServer(t, 0)
	_, code, stderr := accuracyRun(t, server.URL, "wipe")
	if code != 1 || !strings.Contains(stderr, "FAIL") {
		t.Fatalf("exit %d", code)
	}
}

func TestAKubernetesFaultOnALocalSystemEndsTheRunUnknown(t *testing.T) {
	server := kvServer(t, 0)
	_, code, stderr := accuracyRun(t, server.URL, "restart")
	if code != 2 || !strings.Contains(stderr, "microservices.Local cannot restart kv") {
		t.Fatalf("exit %d", code)
	}
}

func TestAKubernetesFaultMustActOnADeploymentTheAppOffers(t *testing.T) {
	system := K8s(k8s.App{Name: "kv", Restartable: []string{"kv"}, Caches: map[string]k8s.CacheKind{"cache": k8s.Redis}}, workload.Get("/"))
	check := func(faults ...Fault) error {
		a := testAccuracy("pause")
		a.Faults = faults
		return a.validate(system)
	}
	if err := check(Restart(0, "kv"), RestartAll(0, "kv"), Exec(0, "cache", "true"), FaultFunc(0, "flush", "elsewhere", func(context.Context) error { return nil })); err != nil {
		t.Fatal(err)
	}
	if err := check(RestartAll(0, "db")); err == nil || !strings.Contains(err.Error(), "restart_all on db: not one of the k8s.App's Restartable") {
		t.Fatalf("got %v", err)
	}
}

func TestATaskWithAnOperationNameLoadgenRefusesIsRefusedBeforeItDeploys(t *testing.T) {
	task := testTask()
	w := *task.Workload
	w.Operations = append([]workload.Operation[*kvModel](nil), w.Operations...)
	w.Operations[0].Name = "config.create"
	task.Workload = &w
	if err := task.validate(); err == nil || !strings.Contains(err.Error(), `operation name "config.create" is not one loadgen takes`) {
		t.Fatalf("got %v", err)
	}
}
