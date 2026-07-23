package telemetry

import (
	"context"
	"os"
	"path/filepath"
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
