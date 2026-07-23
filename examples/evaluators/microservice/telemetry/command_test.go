package telemetry

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCommandCollectorPassesContractPathsAndLoadsReport(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	scriptBody := `#!/bin/sh
set -eu
request=""
output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --request-json) request="$2"; shift 2 ;;
    --output-json) output="$2"; shift 2 ;;
    *) exit 9 ;;
  esac
done
test -s "$request"
cat >"$output" <<'EOF'
{
  "schema_version": 1,
  "source": "test",
  "collected_at": "2026-07-22T12:00:00Z",
  "workload_name": "test",
  "workload_hash": "abc",
  "measurement_windows": [{"start":"2026-07-22T12:00:00Z","end":"2026-07-22T12:00:01Z"}],
  "span_count": 1,
  "error_count": 0,
  "services_by_p95": [{"name":"frontend","count":1,"error_count":0,"mean_ms":2,"p50_ms":2,"p95_ms":2,"p99_ms":2,"max_ms":2}],
  "spans_by_p95": [{"name":"frontend:GET /","count":1,"error_count":0,"mean_ms":2,"p50_ms":2,"p95_ms":2,"p99_ms":2,"max_ms":2}]
}
EOF
`
	if err := os.WriteFile(script, []byte(scriptBody), 0o755); err != nil {
		t.Fatal(err)
	}
	output := filepath.Join(directory, "report.json")
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	report, err := (CommandCollector{
		Command:    []string{script},
		OutputPath: output,
		Timeout:    time.Second,
	}).Collect(context.Background(), CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "test",
		WorkloadHash:  "abc",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if report.SpanCount != 1 || report.Services[0].Name != "frontend" {
		t.Fatalf("report = %+v", report)
	}
}

func TestCommandCollectorRejectsEmptyReport(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	scriptBody := `#!/bin/sh
set -eu
output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --request-json) shift 2 ;;
    --output-json) output="$2"; shift 2 ;;
  esac
done
printf '{"schema_version":1,"source":"test","collected_at":"2026-07-22T12:00:00Z","span_count":0}\n' >"$output"
`
	if err := os.WriteFile(script, []byte(scriptBody), 0o755); err != nil {
		t.Fatal(err)
	}
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	_, err := (CommandCollector{
		Command:    []string{script},
		OutputPath: filepath.Join(directory, "report.json"),
		Timeout:    time.Second,
	}).Collect(context.Background(), CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "test",
		WorkloadHash:  "abc",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	})
	if err == nil {
		t.Fatal("accepted empty telemetry report")
	}
}

func TestCommandCollectorDoesNotReuseStaleReport(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	output := filepath.Join(directory, "report.json")
	if err := os.WriteFile(output, []byte(`{"schema_version":1}`), 0o644); err != nil {
		t.Fatal(err)
	}
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	_, err := (CommandCollector{
		Command:    []string{script},
		OutputPath: output,
		Timeout:    time.Second,
	}).Collect(context.Background(), CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "test",
		WorkloadHash:  "abc",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	})
	if err == nil {
		t.Fatal("collector reused a stale telemetry report")
	}
}

func TestCommandCollectorFailsClosedOnCommandError(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\necho boom >&2\nexit 3\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	_, err := collectWithScript(t, directory, script, time.Second)
	if err == nil {
		t.Fatal("accepted a failed telemetry collector")
	}
	if !strings.Contains(err.Error(), "collector failed") {
		t.Fatalf("error = %v", err)
	}
}

func TestCommandCollectorFailsClosedOnTimeout(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	// exec replaces the shell so the timeout kill reaches sleep directly and the
	// output pipe closes promptly, keeping the test fast.
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexec sleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	_, err := collectWithScript(t, directory, script, 100*time.Millisecond)
	if err == nil {
		t.Fatal("accepted a telemetry collector that exceeded its timeout")
	}
	if !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("error = %v", err)
	}
}

func TestCommandCollectorFailsClosedOnMalformedReport(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	scriptBody := `#!/bin/sh
set -eu
output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --request-json) shift 2 ;;
    --output-json) output="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf 'not json' >"$output"
`
	if err := os.WriteFile(script, []byte(scriptBody), 0o755); err != nil {
		t.Fatal(err)
	}
	_, err := collectWithScript(t, directory, script, time.Second)
	if err == nil {
		t.Fatal("accepted a malformed telemetry report")
	}
	if !strings.Contains(err.Error(), "decode telemetry report") {
		t.Fatalf("error = %v", err)
	}
}

func TestCommandCollectorReportsMissingReport(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	_, err := collectWithScript(t, directory, script, time.Second)
	if err == nil {
		t.Fatal("accepted a collector that wrote no report")
	}
	if !strings.Contains(err.Error(), "did not write a telemetry report") {
		t.Fatalf("error = %v", err)
	}
}

func TestCommandCollectorSucceedsDespiteLingeringChild(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	// The collector writes a valid report, exits 0, but backgrounds a child that
	// inherits and holds the output pipe open. WaitDelay bounds the wait; the
	// report on disk, not the lingering pipe, must decide success.
	scriptBody := `#!/bin/sh
set -eu
output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --request-json) shift 2 ;;
    --output-json) output="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cat >"$output" <<'EOF'
{
  "schema_version": 1,
  "source": "test",
  "collected_at": "2026-07-22T12:00:00Z",
  "workload_name": "test",
  "workload_hash": "abc",
  "measurement_windows": [{"start":"2026-07-22T12:00:00Z","end":"2026-07-22T12:00:01Z"}],
  "span_count": 1,
  "error_count": 0,
  "services_by_p95": [{"name":"frontend","count":1,"error_count":0,"mean_ms":2,"p50_ms":2,"p95_ms":2,"p99_ms":2,"max_ms":2}],
  "spans_by_p95": [{"name":"frontend:GET /","count":1,"error_count":0,"mean_ms":2,"p50_ms":2,"p95_ms":2,"p99_ms":2,"max_ms":2}]
}
EOF
sleep 5 &
exit 0
`
	if err := os.WriteFile(script, []byte(scriptBody), 0o755); err != nil {
		t.Fatal(err)
	}
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	report, err := (CommandCollector{
		Command:    []string{script},
		OutputPath: filepath.Join(directory, "report.json"),
		Timeout:    5 * time.Second,
		WaitDelay:  200 * time.Millisecond,
	}).Collect(context.Background(), CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "test",
		WorkloadHash:  "abc",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	})
	if err != nil {
		t.Fatalf("discarded a valid report because a child held the pipe: %v", err)
	}
	if report.SpanCount != 1 || report.Services[0].Name != "frontend" {
		t.Fatalf("report = %+v", report)
	}
}

func TestCommandCollectorLabelsCancellationNotTimeout(t *testing.T) {
	directory := t.TempDir()
	script := filepath.Join(directory, "collector.sh")
	// exec so the cancellation kill reaches the blocking sleep directly.
	if err := os.WriteFile(script, []byte("#!/bin/sh\nexec sleep 30\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(100 * time.Millisecond)
		cancel()
	}()
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	_, err := (CommandCollector{
		Command:    []string{script},
		OutputPath: filepath.Join(directory, "report.json"),
		Timeout:    0, // no deadline: the collector context is the caller's context
		WaitDelay:  200 * time.Millisecond,
	}).Collect(ctx, CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "test",
		WorkloadHash:  "abc",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	})
	if err == nil {
		t.Fatal("accepted a cancelled collection")
	}
	if !strings.Contains(err.Error(), "cancelled") || strings.Contains(err.Error(), "timed out") {
		t.Fatalf("cancellation mislabeled: %v", err)
	}
}

func TestCommandCollectorFailsClosedOnMissingExecutable(t *testing.T) {
	directory := t.TempDir()
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	_, err := (CommandCollector{
		Command:    []string{filepath.Join(directory, "does-not-exist")},
		OutputPath: filepath.Join(directory, "report.json"),
		Timeout:    time.Second,
	}).Collect(context.Background(), CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "test",
		WorkloadHash:  "abc",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	})
	if err == nil {
		t.Fatal("accepted an unresolvable telemetry collector executable")
	}
	if !strings.Contains(err.Error(), "collector failed") {
		t.Fatalf("error = %v", err)
	}
}

func collectWithScript(t *testing.T, directory, script string, timeout time.Duration) (Report, error) {
	t.Helper()
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	return (CommandCollector{
		Command:    []string{script},
		OutputPath: filepath.Join(directory, "report.json"),
		Timeout:    timeout,
	}).Collect(context.Background(), CollectionRequest{
		SchemaVersion: RequestSchemaVersion,
		WorkloadName:  "test",
		WorkloadHash:  "abc",
		Windows:       []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	})
}

func TestValidateReportRequestRejectsDifferentRun(t *testing.T) {
	start := time.Date(2026, 7, 22, 12, 0, 0, 0, time.UTC)
	request := CollectionRequest{
		WorkloadName: "test", WorkloadHash: "abc",
		Windows: []MeasurementWindow{{Start: start, End: start.Add(time.Second)}},
	}
	report := Report{
		WorkloadName: "test", WorkloadHash: "different",
		Windows: append([]MeasurementWindow(nil), request.Windows...),
	}
	if err := validateReportRequest(report, request); err == nil {
		t.Fatal("accepted telemetry from a different workload")
	}
	report.WorkloadHash = request.WorkloadHash
	report.Windows[0].End = report.Windows[0].End.Add(time.Second)
	if err := validateReportRequest(report, request); err == nil {
		t.Fatal("accepted telemetry from a different measurement window")
	}
}
