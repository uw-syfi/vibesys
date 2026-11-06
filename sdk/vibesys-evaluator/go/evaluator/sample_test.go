package evaluator

import (
	"bytes"
	"context"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/HdrHistogram/hdrhistogram-go"
)

var metrics = map[string]Metric{"tokens_per_s": {Unit: "tok/s", Direction: Max}}

func noSignal() *stopper {
	return &stopper{ctx: context.Background(), signal: func() syscall.Signal { return 0 }}
}

func envOf(dir string) func(string) string {
	return func(k string) string {
		if k == ArtifactsEnv {
			return filepath.Join(dir, "artifacts")
		}
		return ""
	}
}

func benchmark(t *testing.T, fn func(context.Context, *Run) error) (int, string, string) {
	t.Helper()
	dir := t.TempDir()
	out := filepath.Join(dir, "records.jsonl")
	var stderr bytes.Buffer
	code := execute(noSignal(), Task{Metrics: metrics, Benchmark: fn},
		[]string{"benchmark", "--vs-output", out, "--seed", "7"}, envOf(dir), &stderr)
	text, _ := os.ReadFile(out)
	return code, string(text), dir
}

func TestABenchmarkReportsItsMetricsAndKeepsItsPhaseFiles(t *testing.T) {
	code, text, dir := benchmark(t, func(_ context.Context, run *Run) error {
		d, err := run.Phase("load")
		if err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(d, "trace.json"), []byte("{}"), 0o644); err != nil {
			return err
		}
		return run.Report(map[string]float64{"tokens_per_s": 12.5})
	})
	want := `{"kind":"hello","protocol":2,"metrics":{"tokens_per_s":{"direction":"max","unit":"tok/s"}}}` + "\n" +
		`{"kind":"result","label":"","values":{"tokens_per_s":12.5}}` + "\n"
	if code != 0 || text != want {
		t.Fatalf("exit %d, records:\n%s", code, text)
	}
	seeds, _ := os.ReadFile(filepath.Join(dir, "artifacts", "seeds.json"))
	if string(seeds) != "{\"sample_seed\": 7, \"source\": \"--seed\"}\n" {
		t.Errorf("seeds.json = %q", seeds)
	}
	if _, err := os.Stat(filepath.Join(dir, "artifacts/load/trace.json")); err != nil {
		t.Error(err)
	}
}

func TestABenchmarkThatIsNotAMeasurementEndsInAnErrorRecord(t *testing.T) {
	for name, c := range map[string]struct {
		fn    func(context.Context, *Run) error
		named string
	}{
		"no-report":   {func(context.Context, *Run) error { return nil }, "without calling run.report"},
		"unknown":     {func(_ context.Context, r *Run) error { return r.Report(map[string]float64{"other": 1}) }, "declared only ['tokens_per_s']"},
		"phase-name":  {func(_ context.Context, r *Run) error { _, err := r.Phase("Bad Name"); return err }, "phase name"},
		"phase-twice": {func(_ context.Context, r *Run) error { r.Phase("a"); _, err := r.Phase("a"); return err }, "used twice"},
		"panic":       {func(context.Context, *Run) error { var m map[string]int; m["x"] = 1; return nil }, "panic"},
		"sample-error": {func(context.Context, *Run) error {
			return &SampleError{Msg: "the server never answered", Partial: &Partial{Name: "ops", Value: 3, Direction: Max}}
		}, `"message":"the server never answered","partial":{"name":"ops","value":3.0,"direction":"max"}`},
	} {
		code, text, _ := benchmark(t, c.fn)
		if code != 1 || !strings.Contains(text, `"kind":"error"`) || !strings.Contains(text, c.named) {
			t.Errorf("%s: exit %d, records:\n%s", name, code, text)
		}
	}
}

func TestAStopSignalEndsABenchmarkAsAnError(t *testing.T) {
	dir := t.TempDir()
	out := filepath.Join(dir, "records.jsonl")
	ctx, cancel := context.WithCancel(context.Background())
	sig := syscall.Signal(0)
	st := &stopper{ctx: ctx, signal: func() syscall.Signal { return sig }}
	task := Task{Metrics: metrics, Benchmark: func(ctx context.Context, _ *Run) error {
		sig = syscall.SIGTERM
		cancel()
		<-ctx.Done()
		return ctx.Err()
	}}
	code := execute(st, task, []string{"benchmark", "--vs-output", out}, envOf(dir), &bytes.Buffer{})
	text, _ := os.ReadFile(out)
	if code != 1 || !strings.Contains(string(text), "stopped by signal SIGTERM before the sample finished") {
		t.Fatalf("exit %d, records:\n%s", code, text)
	}
}

func TestChecksAndProfilesExitWithTheirVerdict(t *testing.T) {
	dir := t.TempDir()
	run := func(args []string, task Task) int {
		return execute(noSignal(), task, args, envOf(dir), &bytes.Buffer{})
	}
	pass := func(context.Context, *Run) error { return nil }
	fail := func(context.Context, *Run) error { return errors.New("wrong") }
	if run([]string{"check"}, Task{Check: pass}) != 0 || run([]string{"check"}, Task{Check: fail}) != 1 {
		t.Error("a check exits 0 on nil and 1 on a reason")
	}
	if run([]string{"check"}, Task{}) != 2 || run([]string{"profile"}, Task{}) != 2 {
		t.Error("a mode the task does not define exits 2")
	}
	var reported error
	profile := func(_ context.Context, r *Run) error {
		reported = r.Report(map[string]float64{"tokens_per_s": 1})
		d, _ := r.Phase("profile-load")
		return os.WriteFile(filepath.Join(d, "done"), nil, 0o644)
	}
	if run([]string{"profile"}, Task{Profile: profile, Metrics: metrics}) != 0 {
		t.Error("profile failed")
	}
	if reported == nil || reported.Error() != "run.report: the profile mode is never scored; report only in benchmark" {
		t.Errorf("report in a profile: %v", reported)
	}
	if _, err := os.Stat(filepath.Join(dir, "artifacts/profile-load/done")); err != nil {
		t.Error(err)
	}
}

func TestUsageErrorsExit2(t *testing.T) {
	for _, args := range [][]string{nil, {"bogus"}, {"benchmark"}, {"benchmark", "--vs-output", "x", "--seed", "abc"}, {"check", "--seed", "1"}, {"benchmark", "--vs-output"}} {
		var stderr bytes.Buffer
		task := Task{Metrics: metrics, Benchmark: func(context.Context, *Run) error { return nil }, Check: func(context.Context, *Run) error { return nil }}
		if code := execute(noSignal(), task, args, envOf(t.TempDir()), &stderr); code != 2 {
			t.Errorf("%v: exit %d", args, code)
		}
	}
}

// The libraries' own percentile (for q > 0) agrees with ours on every fixture histogram.
func TestPercentilesAgreeWithHdrhistogramGo(t *testing.T) {
	f := loadFixture(t)
	for _, c := range f.Histograms {
		raw, _ := base64.StdEncoding.DecodeString(c.Encoded)
		ref, err := hdrhistogram.Decode([]byte(c.Encoded))
		if err != nil {
			t.Fatalf("%s: %v", c.Name, err)
		}
		ours, err := DecodeHistogram(raw)
		if err != nil {
			t.Fatal(err)
		}
		if c.Count == 0 {
			continue
		}
		for _, p := range []float64{1, 25, 50, 75, 90, 99, 99.9, 100} {
			if got, want := ours.Percentile(p), ref.ValueAtQuantile(p); got != want {
				t.Errorf("%s p%v: ours %d, hdrhistogram-go %d", c.Name, p, got, want)
			}
		}
	}
}
