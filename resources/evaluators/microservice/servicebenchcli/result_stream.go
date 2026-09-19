package servicebenchcli

import (
	"errors"
	"fmt"

	"github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/engine"
)

// benchmarkStream is the framework-facing output of benchmark mode: the
// VibeSys evaluator record stream described by sdk/vs-evaluator/PROTOCOL.md.
// It declares the workload's own objective metric and carries either the
// summary's primary value or the reason no value exists.
//
// The objective metric is the only required one, because it is the only
// quantity every accepted run produces. The latency percentiles the summary
// measures alongside it are declared optional, so they reach the framework
// when they exist without a run that measured nothing failing the protocol.
// Everything else the summary holds (per-trial distributions, generator
// health, telemetry) stays in the --output-json report, which is diagnostics
// rather than a measurement contract.
//
// Without --vs-output the SDK report discards every record, so standalone
// invocations that only want the printed summary and the --output-json report
// behave exactly as before.
type benchmarkStream struct {
	report  *vseval.Report
	run     *vseval.Run
	metric  vseval.Metric
	latency []latencyMetric
}

// latencyMetric is one optional latency axis: the name the framework sees and
// the percentile of the run's latency distribution that carries it.
type latencyMetric struct {
	name   string
	pick   func(engine.Distribution) *float64
	handle vseval.Metric
}

// latencyAxes are the latency percentiles worth optimizing against: the
// typical request and the tail. The summary already measures both over every
// successful operation, and the names match how a workload objective spells
// them. The remaining percentiles stay in the --output-json report; declaring
// all of them would be a wall of near-duplicate axes.
var latencyAxes = []latencyMetric{
	{name: "latency_ms.p50", pick: func(d engine.Distribution) *float64 { return d.P50 }},
	{name: "latency_ms.p99", pick: func(d engine.Distribution) *float64 { return d.P99 }},
}

// openBenchmarkStream opens the record stream at outputPath, or opens one that
// discards every record when outputPath is empty.
//
// Opening happens as soon as the argv is parsed, before the workload is loaded,
// because the failures in between are exactly the ones the framework used to
// lose: a workload that does not parse or does not validate leaves a reason on
// the stream rather than no file at all. The metrics come from that workload,
// so declaring them waits for it.
func openBenchmarkStream(outputPath string) (*benchmarkStream, error) {
	report, err := vseval.OpenPath(outputPath)
	if err != nil {
		return nil, err
	}
	return &benchmarkStream{report: report}, nil
}

// declare writes the hello record for the resolved workload's objective.
//
// The declared name, unit, and direction are the workload's own: an objective
// with metric "operations_per_second" reaches the framework under that name
// rather than under a generic result field. Each latency axis the objective
// does not already claim is declared beside it as an optional metric. The
// hello record lands before anything is measured, so a crashed or timed-out
// benchmark leaves a stream that names its metrics.
func (s *benchmarkStream) declare(objective api.Objective) error {
	direction, err := vseval.ParseDir(objective.Direction)
	if err != nil {
		return err
	}
	schema := vseval.NewSchema()
	s.metric = schema.Number(
		objective.Metric,
		vseval.Unit(objective.Unit),
		vseval.Direction(direction),
	)
	for _, axis := range latencyAxes {
		if axis.name == objective.Metric {
			// The objective already reports this percentile, and it reports it
			// as a required metric.
			continue
		}
		axis.handle = schema.Number(
			axis.name,
			vseval.Unit("ms"),
			vseval.Direction(vseval.Min),
			vseval.Optional(),
		)
		s.latency = append(s.latency, axis)
	}
	run, err := s.report.Declare(schema)
	if err != nil {
		return err
	}
	s.run = run
	return nil
}

// emit writes the summary's primary value, and whichever declared latency
// percentiles the run produced, as the stream's result record.
//
// A run the engine rejected, or one that produced no primary value, is a
// failure rather than a measurement: emit returns the reason and leaves the
// error record to finish, so the command's exit status and stderr keep the
// wording they had before the stream existed.
func (s *benchmarkStream) emit(summary engine.Summary) error {
	if !summary.Valid {
		return errors.New("benchmark result is invalid; inspect constraints and trial invalid_reasons")
	}
	if summary.PrimaryValue == nil {
		return fmt.Errorf("benchmark produced no %s value", summary.PrimaryMetric.Metric)
	}
	s.run.Set(s.metric, *summary.PrimaryValue)
	latency := summary.LatencyMS()
	for _, axis := range s.latency {
		// An optional metric is left unset when the run measured no latency,
		// which is what makes it optional.
		if value := axis.pick(latency); value != nil {
			s.run.Set(axis.handle, *value)
		}
	}
	return s.run.Emit()
}

// finish reports cause as the stream's error record and returns it unchanged.
// Deferring it covers every failure the command can return once the stream is
// open, including the ones managed-candidate cleanup adds on the way out,
// without each return site having to know about the stream.
//
// A command that returns nothing and reported nothing leaves a stream holding
// only its hello record, which a reader reports as a missing outcome. That is
// the honest report: no measurement was made.
func (s *benchmarkStream) finish(cause error) error {
	if cause == nil || s.report.OutcomeWritten() {
		return cause
	}
	if err := s.report.EmitError(cause); err != nil {
		return fmt.Errorf("%w (reporting the failure also failed: %v)", cause, err)
	}
	return cause
}

// Close releases the output file. It is the only close in the command: the SDK
// leaves it to whoever opened the report.
func (s *benchmarkStream) Close() error {
	return s.report.Close()
}
