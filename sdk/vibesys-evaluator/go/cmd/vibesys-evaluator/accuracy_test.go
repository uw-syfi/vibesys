package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/phase"
)

// The test binary plays a task's commands in an accuracy run: its phase (bench.py phase: the
// core's phase pod, here with the fake loadgen), its seed and its oracle (exiting with
// $ACC_TEST_VERDICT and recording its argv).
var accuracyRoles = map[string]func(args []string) int{
	"acc-test-phase": func(args []string) int {
		flag := func(name string) string { return args[slices.Index(args, name)+1] }
		exe, _ := os.Executable()
		req, _ := json.Marshal(map[string]any{"work_dir": flag("--work"), "target": flag("--target"),
			"generator": []string{"/bin/generator"}, "dataset_file": "/data/dataset.json", "loadgen": exe})
		os.Setenv(fakeLoadgenEnv, "1")
		var stdout bytes.Buffer
		return run([]string{"phase", "pod"}, bytes.NewReader(req), &stdout, os.Stderr)
	},
	"acc-test-seed": func([]string) int { fmt.Println("seeded"); return 0 },
	"acc-test-oracle": func(args []string) int {
		code, _ := strconv.Atoi(os.Getenv("ACC_TEST_VERDICT"))
		_ = os.WriteFile(filepath.Join(args[slices.Index(args, "--out")+1], "oracle-argv.json"), must(json.Marshal(args)), 0o644)
		return code
	},
}

func accuracyRequest(t *testing.T, faults string) (string, string) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	history, out := filepath.Join(dir, "history"), filepath.Join(dir, "out")
	if err := os.MkdirAll(out, 0o755); err != nil {
		t.Fatal(err)
	}
	return dir, fmt.Sprintf(`{"local":{"target":"http://127.0.0.1:1","seed":[%[1]s,"acc-test-seed"]},
		"phase_command":[%[1]s,"acc-test-phase"],"sample_seed":"7","phases":2,
		"load":{"model":"LOAD_MODEL_OPEN","rate":10,"arrivals":"ARRIVALS_UNIFORM","secs":1},
		"faults":[%[2]s],"history_dir":%[3]s,"oracle":[%[1]s,"acc-test-oracle"],"out_dir":%[4]s}`,
		jsonQuote(exe), faults, jsonQuote(history), jsonQuote(out))
}

func TestAnAccuracyRunRecordsTheHistoryAndRunsTheOracle(t *testing.T) {
	t.Setenv("ACC_TEST_VERDICT", "1")
	dir, req := accuracyRequest(t, "")
	// An older wrapper's validity (field 8, now reserved) is ignored.
	req = strings.Replace(req, `"sample_seed"`, `"validity":{"max_error_rate":0.001},"sample_seed"`, 1)
	var stdout, stderr bytes.Buffer
	code := run([]string{"accuracy", "run"}, strings.NewReader(req), &stdout, &stderr)
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	if code != 0 {
		t.Fatalf("exit %d: %s\n%s", code, stdout.String(), stderr.String())
	}
	var resp map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &resp); err != nil {
		t.Fatal(err)
	}
	if resp["verdict"] != "FAIL" || resp["exit_code"] != 1.0 || resp["phases"] != 2.0 {
		t.Fatalf("response %v", resp)
	}
	var stages []string
	for _, line := range lines[:len(lines)-1] {
		var event struct {
			Event *struct{ Stage, Message string }
		}
		if err := json.Unmarshal([]byte(line), &event); err != nil || event.Event == nil {
			t.Fatalf("line %q is not an event", line)
		}
		stages = append(stages, event.Event.Stage)
	}
	if stages[len(stages)-2] != "check" || stages[len(stages)-1] != "done" {
		t.Fatalf("stages %v", stages)
	}
	// The phases are not judged as measurements: each logs what it sent.
	if !strings.Contains(stdout.String(), "operations sent") || strings.Contains(stdout.String(), "not a measurement") {
		t.Fatalf("events %s", stdout.String())
	}
	manifest := &pb.HistoryManifest{}
	if err := proto.Unmarshal(must(os.ReadFile(filepath.Join(dir, "history", phase.HistoryFile))), manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.GetPhases() != 2 {
		t.Fatalf("manifest %v", manifest)
	}
	for _, name := range []string{"phase-0/results.binpb", "phase-1/kept.binpb", "phase-1/summary.binpb"} {
		if _, err := os.Stat(filepath.Join(dir, "history", name)); err != nil {
			t.Error(err)
		}
	}
	var argv []string
	if err := json.Unmarshal(must(os.ReadFile(filepath.Join(dir, "out", "oracle-argv.json"))), &argv); err != nil {
		t.Fatal(err)
	}
	if want := []string{"--history", filepath.Join(dir, "history"), "--out", filepath.Join(dir, "out")}; !slices.Equal(argv, want) {
		t.Fatalf("oracle argv %q", argv)
	}
	if got := string(must(os.ReadFile(filepath.Join(dir, "seed.log")))); got != "seeded\n" {
		t.Fatalf("seed log %q", got)
	}
}

func TestAnAccuracyRunEndsAtAFaultItCannotApply(t *testing.T) {
	for action, want := range map[string]string{
		"RESTART": "a local system cannot restart kv",
		"FLUSH":   "a local system cannot flush kv",
	} {
		_, req := accuracyRequest(t, `{"after_phase":0,"action":"ACCURACY_FAULT_ACTION_`+action+`","deployment":"kv"}`)
		code, out := lastLine(t, req)
		if code != 1 || out["error_kind"] != "run" || !strings.Contains(out["message"].(string), want) {
			t.Fatalf("%s: exit %d, %v", action, code, out)
		}
	}
}

func TestAnAccuracyRunRefusesABadPlan(t *testing.T) {
	for faults, want := range map[string]string{
		`{"after_phase":1,"action":"ACCURACY_FAULT_ACTION_RESTART","deployment":"kv"}`:            "between two of the 2 phases",
		`{"after_phase":0,"action":"ACCURACY_FAULT_ACTION_EXEC","deployment":"kv"}`:               "has no argv",
		`{"after_phase":0,"deployment":"kv"}`:                                                     "action must be",
		`{"after_phase":0,"action":"ACCURACY_FAULT_ACTION_FLUSH","deployment":"kv","argv":["x"]}`: "only an exec fault runs a command",
	} {
		_, req := accuracyRequest(t, faults)
		code, out := lastLine(t, req)
		if code != 1 || out["error_kind"] != "bad_request" || !strings.Contains(out["message"].(string), want) {
			t.Errorf("%s: exit %d, %v", faults, code, out)
		}
	}
}

func TestAnAccuracyRunRefusesAFaultOnADeploymentTheAppDoesNotOffer(t *testing.T) {
	_, req := accuracyRequest(t, `{"after_phase":0,"action":"ACCURACY_FAULT_ACTION_RESTART","deployment":"db"}`)
	req = strings.Replace(req, `"local":{"target":"http://127.0.0.1:1",`, `"k8s":{"app":{"name":"kv","restartable":["kv"]}},"unused":{`, 1)
	code, out := lastLine(t, req)
	if code != 1 || out["error_kind"] != "bad_request" || !strings.Contains(out["message"].(string), "restart on db: not one of the App's restartable") {
		t.Fatalf("exit %d, %v", code, out)
	}
	// A flush empties a cache: a restartable Deployment that is not one is refused.
	_, req = accuracyRequest(t, `{"after_phase":0,"action":"ACCURACY_FAULT_ACTION_FLUSH","deployment":"kv"}`)
	req = strings.Replace(req, `"local":{"target":"http://127.0.0.1:1",`, `"k8s":{"app":{"name":"kv","restartable":["kv"],"caches":{"cache":"K8S_CACHE_KIND_REDIS"}}},"unused":{`, 1)
	code, out = lastLine(t, req)
	if code != 1 || out["error_kind"] != "bad_request" || !strings.Contains(out["message"].(string), "flush on kv: not one of the App's caches") {
		t.Fatalf("exit %d, %v", code, out)
	}
}

func lastLine(t *testing.T, req string) (int, map[string]any) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := run([]string{"accuracy", "run"}, strings.NewReader(req), &stdout, &stderr)
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	var out map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &out); err != nil {
		t.Fatalf("%q: %v", stdout.String(), err)
	}
	return code, out
}
