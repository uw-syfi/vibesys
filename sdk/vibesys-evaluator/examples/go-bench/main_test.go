package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// TestMain lets the tests run this program's own main: with the variable set the test binary is
// the benchmark script.
func TestMain(m *testing.M) {
	if os.Getenv("GO_BENCH_RUN_MAIN") != "" {
		main()
		return
	}
	os.Exit(m.Run())
}

func script(t *testing.T, args ...string) (string, error) {
	t.Helper()
	cmd := exec.Command(os.Args[0], args...)
	cmd.Env = append(os.Environ(), "GO_BENCH_RUN_MAIN=1", "VIBESYS_ARTIFACTS_DIR="+filepath.Join(t.TempDir(), "artifacts"))
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func TestTheBenchmarkWritesAHelloAndAResult(t *testing.T) {
	records := filepath.Join(t.TempDir(), "records.jsonl")
	if out, err := script(t, "benchmark", "--vs-output", records, "--seed", "1"); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
	data, _ := os.ReadFile(records)
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 || !strings.HasPrefix(lines[0], `{"kind":"hello","protocol":2`) || !strings.HasPrefix(lines[1], `{"kind":"result","label":"","values":{"hash_mb_per_s":`) {
		t.Fatalf("records:\n%s", data)
	}
}

func TestTheCheckPasses(t *testing.T) {
	if out, err := script(t, "check"); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
}
