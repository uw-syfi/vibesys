package servicebenchcli

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/engine"
)

// trainTicketObjective is the objective the committed Train Ticket workload
// declares. Its name and its metric differ on purpose: the stream declares the
// metric, which is the quantity the summary measures.
func trainTicketObjective() api.Objective {
	return api.Objective{
		Name:      "logical_operations_per_second",
		Metric:    "operations_per_second",
		Direction: "maximize",
		Unit:      "operations/s",
	}
}

func pointer(value float64) *float64 { return &value }

func measuredSummary(objective api.Objective, value float64) engine.Summary {
	return engine.Summary{
		SchemaVersion: engine.ResultSchemaVersion,
		WorkloadName:  "train-ticket",
		PrimaryValue:  &value,
		PrimaryMetric: objective,
		Valid:         true,
		Aggregate:     engine.Aggregate{Trials: 3, Median: &value},
	}
}

// withLatency gives a summary the per-trial latency distribution a run that
// completed operations measures.
func withLatency(summary engine.Summary, p50 float64, p99 float64) engine.Summary {
	summary.Trials = []engine.TrialResult{{
		Valid:     true,
		LatencyMS: engine.Distribution{Count: 128, P50: pointer(p50), P99: pointer(p99)},
	}}
	return summary
}

// openDeclared opens a stream at path and declares objective on it, which is
// the state the command reaches once its workload validates.
func openDeclared(t *testing.T, objective api.Objective, path string) *benchmarkStream {
	t.Helper()
	stream, err := openBenchmarkStream(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { stream.Close() })
	if err := stream.declare(objective); err != nil {
		t.Fatal(err)
	}
	return stream
}

func readStreamRecords(t *testing.T, path string) []map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	records := make([]map[string]any, 0, 2)
	for _, line := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			t.Fatalf("record %q: %v", line, err)
		}
		records = append(records, record)
	}
	return records
}

func metricSpecs(t *testing.T, hello map[string]any) map[string]any {
	t.Helper()
	metrics, ok := hello["metrics"].(map[string]any)
	if !ok {
		t.Fatalf("hello record = %v, want declared metrics", hello)
	}
	return metrics
}

func metricSpec(t *testing.T, hello map[string]any, name string) map[string]any {
	t.Helper()
	metrics := metricSpecs(t, hello)
	spec, ok := metrics[name].(map[string]any)
	if !ok {
		t.Fatalf("hello metrics = %v, want %q", metrics, name)
	}
	return spec
}

func resultValues(t *testing.T, record map[string]any) map[string]any {
	t.Helper()
	values, ok := record["values"].(map[string]any)
	if !ok {
		t.Fatalf("result record = %v, want measured values", record)
	}
	return values
}

// summaryPrimaryValue reads primary_value out of the summary exactly as the
// --output-json report serializes it, so the stream can be compared against
// the number the diagnostic report publishes rather than against the Go field.
func summaryPrimaryValue(t *testing.T, summary engine.Summary) any {
	t.Helper()
	encoded, err := json.MarshalIndent(summary, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	var report map[string]any
	if err := json.Unmarshal(encoded, &report); err != nil {
		t.Fatal(err)
	}
	return report["primary_value"]
}

func TestBenchmarkStreamDeclaresWorkloadObjective(t *testing.T) {
	objective := trainTicketObjective()
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, objective, streamPath)
	summary := measuredSummary(objective, 412.5)
	if err := stream.emit(summary); err != nil {
		t.Fatal(err)
	}

	records := readStreamRecords(t, streamPath)
	if len(records) != 2 {
		t.Fatalf("stream records = %v, want hello and result", records)
	}
	hello := records[0]
	if hello["kind"] != "hello" || hello["protocol"] != float64(2) {
		t.Fatalf("hello record = %v", hello)
	}
	spec := metricSpec(t, hello, objective.Metric)
	if spec["unit"] != objective.Unit || spec["direction"] != "max" {
		t.Fatalf("%s spec = %v", objective.Metric, spec)
	}
	// The objective metric is the one every accepted run produces, so it is
	// declared required, which the protocol spells by omitting the key.
	if _, present := spec["required"]; present {
		t.Fatalf("%s spec = %v, want no required key", objective.Metric, spec)
	}

	result := records[1]
	if result["kind"] != "result" || result["label"] != "" {
		t.Fatalf("result record = %v", result)
	}
	values := resultValues(t, result)
	if values[objective.Metric] != summaryPrimaryValue(t, summary) {
		t.Fatalf(
			"streamed %s = %v, summary primary_value = %v",
			objective.Metric,
			values[objective.Metric],
			summaryPrimaryValue(t, summary),
		)
	}
}

func TestBenchmarkStreamDeclaresMinimizedObjective(t *testing.T) {
	objective := api.Objective{
		Name:      "read_latency_p50",
		Metric:    "latency_ms.p50",
		Direction: "minimize",
		Unit:      "ms",
	}
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, objective, streamPath)
	if err := stream.emit(measuredSummary(objective, 12.5)); err != nil {
		t.Fatal(err)
	}

	records := readStreamRecords(t, streamPath)
	spec := metricSpec(t, records[0], objective.Metric)
	if spec["unit"] != "ms" || spec["direction"] != "min" {
		t.Fatalf("%s spec = %v", objective.Metric, spec)
	}
	// An objective that already reports a latency axis reports it as the
	// required metric; declaring the same name again would be a duplicate.
	if _, present := spec["required"]; present {
		t.Fatalf("%s spec = %v, want the objective declared required", objective.Metric, spec)
	}
	metrics := metricSpecs(t, records[0])
	if len(metrics) != 2 {
		t.Fatalf("hello metrics = %v, want the objective and the remaining latency axis", metrics)
	}
}

// TestBenchmarkStreamDeclaresLatencyAxesAsOptional covers the metrics the
// summary measures beside the objective. They are optional because a run that
// completed no operation measures no latency, and a metric a successful run
// can omit must say so in its declaration.
func TestBenchmarkStreamDeclaresLatencyAxesAsOptional(t *testing.T) {
	objective := trainTicketObjective()
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, objective, streamPath)
	summary := withLatency(measuredSummary(objective, 412.5), 18.5, 96.25)
	if err := stream.emit(summary); err != nil {
		t.Fatal(err)
	}

	records := readStreamRecords(t, streamPath)
	metrics := metricSpecs(t, records[0])
	if len(metrics) != 3 {
		t.Fatalf("hello metrics = %v, want the objective and both latency axes", metrics)
	}
	for name, want := range map[string]float64{"latency_ms.p50": 18.5, "latency_ms.p99": 96.25} {
		spec := metricSpec(t, records[0], name)
		if spec["unit"] != "ms" || spec["direction"] != "min" || spec["required"] != false {
			t.Fatalf("%s spec = %v, want an optional millisecond axis", name, spec)
		}
		if got := resultValues(t, records[1])[name]; got != want {
			t.Fatalf("streamed %s = %v, want %v", name, got, want)
		}
	}
}

// TestBenchmarkStreamOmitsUnmeasuredLatency is the other half of declaring the
// latency axes optional: a run that measured none of them still emits a valid
// result row rather than failing on a metric it cannot produce.
func TestBenchmarkStreamOmitsUnmeasuredLatency(t *testing.T) {
	objective := trainTicketObjective()
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, objective, streamPath)
	if err := stream.emit(measuredSummary(objective, 412.5)); err != nil {
		t.Fatal(err)
	}

	records := readStreamRecords(t, streamPath)
	values := resultValues(t, records[1])
	if len(values) != 1 {
		t.Fatalf("result values = %v, want only %q", values, objective.Metric)
	}
	if _, present := values["latency_ms.p99"]; present {
		t.Fatalf("result values = %v, want no unmeasured latency", values)
	}
}

// TestBenchmarkStreamReportsFailureBeforeDeclaring is the reason the stream
// opens before the workload loads: a workload that does not parse, does not
// validate, or names a direction the protocol cannot express has no schema to
// declare, and an error record on its own is a complete stream.
func TestBenchmarkStreamReportsFailureBeforeDeclaring(t *testing.T) {
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream, err := openBenchmarkStream(streamPath)
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()

	cause := errors.New("load workload: benchmark/workload.toml: missing objective")
	if got := stream.finish(cause); !errors.Is(got, cause) {
		t.Fatalf("finish(%v) = %v, want the cause unchanged", cause, got)
	}

	records := readStreamRecords(t, streamPath)
	if len(records) != 1 {
		t.Fatalf("stream records = %v, want a single error", records)
	}
	if records[0]["kind"] != "error" || records[0]["message"] != cause.Error() {
		t.Fatalf("error record = %v, want %q", records[0], cause)
	}
}

func TestBenchmarkStreamRejectsUnknownDirection(t *testing.T) {
	objective := trainTicketObjective()
	objective.Direction = "sideways"
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream, err := openBenchmarkStream(streamPath)
	if err != nil {
		t.Fatal(err)
	}
	defer stream.Close()

	declareErr := stream.declare(objective)
	if declareErr == nil {
		t.Fatal("accepted an objective direction the protocol cannot declare")
	}
	if got := stream.finish(declareErr); !errors.Is(got, declareErr) {
		t.Fatalf("finish(%v) = %v, want the cause unchanged", declareErr, got)
	}

	records := readStreamRecords(t, streamPath)
	if len(records) != 1 || records[0]["kind"] != "error" {
		t.Fatalf("stream records = %v, want a single error and no hello", records)
	}
}

func TestBenchmarkStreamReportsInvalidResultAsErrorRecord(t *testing.T) {
	objective := trainTicketObjective()
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, objective, streamPath)
	summary := measuredSummary(objective, 412.5)
	summary.Valid = false
	summary.PrimaryValue = nil
	summary.Constraints = engine.ConstraintResult{
		Passed:  false,
		Reasons: []string{"trial 0: error rate 0.02 exceeds 0.00"},
	}
	emitErr := stream.emit(summary)
	if emitErr == nil {
		t.Fatal("invalid summary was reported as a measurement")
	}
	// The command reports the failure the way it always has; the stream picks
	// the same reason up from the returned error.
	if got := stream.finish(emitErr); !errors.Is(got, emitErr) {
		t.Fatalf("finish(%v) = %v, want the cause unchanged", emitErr, got)
	}

	records := readStreamRecords(t, streamPath)
	if len(records) != 2 {
		t.Fatalf("stream records = %v, want hello and error", records)
	}
	if records[0]["kind"] != "hello" {
		t.Fatalf("first record = %v, want hello", records[0])
	}
	if records[1]["kind"] != "error" || records[1]["message"] != emitErr.Error() {
		t.Fatalf("second record = %v, want the error %q", records[1], emitErr)
	}
}

func TestBenchmarkStreamReportsAbsentPrimaryValueAsErrorRecord(t *testing.T) {
	objective := trainTicketObjective()
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, objective, streamPath)
	// A summary the engine accepted but that carries no aggregate value has no
	// row to report, and a Go zero must not pass for a measurement.
	summary := measuredSummary(objective, 0)
	summary.PrimaryValue = nil
	summary.Aggregate = engine.Aggregate{Trials: 3}
	emitErr := stream.emit(summary)
	if emitErr == nil {
		t.Fatal("summary without a primary value was reported as a measurement")
	}
	if !strings.Contains(emitErr.Error(), objective.Metric) {
		t.Fatalf("error = %v, want it to name %q", emitErr, objective.Metric)
	}
	if got := stream.finish(emitErr); !errors.Is(got, emitErr) {
		t.Fatalf("finish(%v) = %v, want the cause unchanged", emitErr, got)
	}

	records := readStreamRecords(t, streamPath)
	if len(records) != 2 || records[1]["kind"] != "error" {
		t.Fatalf("stream records = %v, want hello and error", records)
	}
	if records[1]["message"] != emitErr.Error() {
		t.Fatalf("error record message = %v, want %q", records[1]["message"], emitErr)
	}
}

// TestBenchmarkStreamFinishReportsCommandFailure covers the failures that
// never reach emit: a candidate that does not start, a telemetry collector
// fault, a cleanup error on the way out. The deferred finish turns each into
// the stream's error record.
func TestBenchmarkStreamFinishReportsCommandFailure(t *testing.T) {
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, trainTicketObjective(), streamPath)
	cause := errors.New("prepare managed candidate: readiness probe timed out")
	if got := stream.finish(cause); !errors.Is(got, cause) {
		t.Fatalf("finish(%v) = %v, want the cause unchanged", cause, got)
	}
	// A second failure on the way out must not append a second outcome record.
	if got := stream.finish(errors.New("close managed candidate: exit status 1")); got == nil {
		t.Fatal("finish swallowed a later failure")
	}

	records := readStreamRecords(t, streamPath)
	if len(records) != 2 {
		t.Fatalf("stream records = %v, want hello and error", records)
	}
	if records[1]["kind"] != "error" || records[1]["message"] != cause.Error() {
		t.Fatalf("error record = %v, want %q", records[1], cause)
	}
}

// TestBenchmarkStreamAfterEmitKeepsTheResult guards the ordering of the
// deferred finish against the deferred managed-candidate cleanup: a cleanup
// failure after a measured row must not overwrite the row with an error.
func TestBenchmarkStreamAfterEmitKeepsTheResult(t *testing.T) {
	objective := trainTicketObjective()
	streamPath := filepath.Join(t.TempDir(), "stream.jsonl")

	stream := openDeclared(t, objective, streamPath)
	if err := stream.emit(measuredSummary(objective, 412.5)); err != nil {
		t.Fatal(err)
	}

	cause := errors.New("close managed candidate: exit status 1")
	if got := stream.finish(cause); !errors.Is(got, cause) {
		t.Fatalf("finish(%v) = %v, want the cause unchanged", cause, got)
	}
	records := readStreamRecords(t, streamPath)
	if len(records) != 2 || records[1]["kind"] != "result" {
		t.Fatalf("stream records = %v, want hello and result", records)
	}
}

func TestBenchmarkWithoutStreamFlagWritesNothing(t *testing.T) {
	objective := trainTicketObjective()
	outputs := t.TempDir()

	stream := openDeclared(t, objective, "")
	if stream.report.Reporting() {
		t.Fatal("omitting the output path still reports a stream")
	}
	if err := stream.emit(measuredSummary(objective, 412.5)); err != nil {
		t.Fatal(err)
	}
	cause := errors.New("benchmark result is invalid")
	if got := stream.finish(cause); !errors.Is(got, cause) {
		t.Fatalf("finish(%v) = %v, want the cause unchanged", cause, got)
	}

	entries, err := os.ReadDir(outputs)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("omitting the output path wrote files: %v", entries)
	}
}
