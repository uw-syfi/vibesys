package vseval

import (
	"fmt"
	"math"
	"strings"

	evaluatorv1 "github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval/internal/gen/vibesys/evaluator/v1"
)

// Run is a declared evaluator report. Its hello record is already written;
// what remains is to set the declared metrics and call [Run.Emit], or to
// report a failure with [Run.EmitError] or [Run.Fail].
//
// A Run is not safe for concurrent use.
type Run struct {
	report *Report
	schema *Schema
	values []float64
	set    []bool
	err    error
}

// Set records the measured value of a declared metric.
//
// Setting the same metric twice is allowed and the last write wins;
// evaluators often refine a value as a run proceeds. Errors (a handle from a
// different schema, or a NaN or infinite value) are held and returned by
// [Run.Emit], so that measurement code stays free of error plumbing.
func (r *Run) Set(m Metric, value float64) {
	if m.schema != r.schema {
		r.setErr(fmt.Errorf("metric %q was declared on a different schema", m.name))
		return
	}
	if m.index >= len(r.values) {
		r.setErr(fmt.Errorf("metric %q was declared after the schema, so it is not in the hello record", m.name))
		return
	}
	if math.IsNaN(value) {
		r.setErr(fmt.Errorf("metric %q was set to NaN, which is not a finite JSON number", m.name))
		return
	}
	if math.IsInf(value, 0) {
		r.setErr(fmt.Errorf("metric %q was set to %v, which is not a finite JSON number", m.name, value))
		return
	}
	r.values[m.index] = value
	r.set[m.index] = true
}

// Emit writes the result record and flushes it. It does not close the output:
// the caller that opened the report owns the close, normally as a deferred
// [Report.Close].
//
// It fails, writing nothing, if a required metric was never set, or if any
// [Run.Set] call was rejected. An unset required metric is an error rather
// than a zero: Go's zero value must not pass for a measurement. A metric
// declared with [Optional] may be absent, and is then left out of the row.
// After a failed Emit no outcome has been written, so the caller can still
// report the failure with [Run.EmitError] or [Run.Fail].
func (r *Run) Emit() error {
	if r.report.outcome {
		return errOutcomeWritten
	}
	if r.err != nil {
		return r.err
	}
	// A metric declared after the hello record was written is a schema error
	// even if its handle was never set.
	if err := r.schema.Err(); err != nil {
		return err
	}
	var missing []string
	values := make(map[string]float64, len(r.values))
	for i := range r.set {
		switch {
		case r.set[i]:
			values[r.schema.names[i]] = r.values[i]
		case r.schema.specs[i].required():
			missing = append(missing, r.schema.names[i])
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("required metrics were never set: %s", strings.Join(missing, ", "))
	}
	if err := r.report.write(&evaluatorv1.ResultRecord{Kind: ptr(kindResult), Label: ptr(""), Values: values}); err != nil {
		return err
	}
	r.report.outcome = true
	return nil
}

// EmitError writes an error record on this run's report. See
// [Report.EmitError].
func (r *Run) EmitError(cause error) error { return r.report.EmitError(cause) }

// Fail writes an error record on this run's report and terminates the process.
// See [Report.Fail].
func (r *Run) Fail(cause error) { r.report.Fail(cause) }

// Reporting reports whether this run writes a stream anyone will read. See
// [Report.Reporting].
func (r *Run) Reporting() bool { return r.report.Reporting() }

// OutcomeWritten reports whether the stream already carries its outcome. See
// [Report.OutcomeWritten].
func (r *Run) OutcomeWritten() bool { return r.report.OutcomeWritten() }

func (r *Run) setErr(err error) {
	if r.err == nil {
		r.err = err
	}
}
