package vseval

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	evaluatorv1 "github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval/internal/gen/vibesys/evaluator/v1"
)

// Protocol is the record stream protocol version this package writes.
const Protocol = 2

// OutputFlag is the name of the flag naming the output file. Callers spell it
// -vs-output or --vs-output; both forms are accepted by the flag package.
const OutputFlag = "vs-output"

const outputFlagUsage = "path of the VibeSys evaluator result stream to write (required)"

// FailureFallbackMessage is the error record message written when a caller
// fails a report without a usable one. The protocol requires a non-empty
// message, so a nil or blank cause must not be allowed to produce an invalid
// stream.
const FailureFallbackMessage = "evaluator failed without a message"

// Report is an open evaluator result stream whose schema is not declared yet.
// It is the first of the two reporting phases: the output is open, so a
// failure can be reported from here on, and [Report.Declare] turns it into a
// [Run] once the metrics are known.
//
// Opening the output before the schema exists is what lets an evaluator whose
// metric identity comes from configuration report a failure that happens while
// loading that configuration. An error record with no preceding hello is a
// valid stream; a missing file is not.
//
// A Report is not safe for concurrent use.
type Report struct {
	w         io.Writer
	closer    io.Closer
	reporting bool
	declared  bool
	outcome   bool
	exit      func(int)
}

// Open parses the process flags if they are not parsed yet and opens the file
// named by -vs-output. It is the entry point for an evaluator with a single
// command; one with subcommands should use [OpenFlagSet] instead, and one that
// parses its own flags should use [OpenPath].
//
// Open registers -vs-output on flag.CommandLine. Callers that parse
// flag.CommandLine themselves must call [RegisterFlags] before their own
// flag.Parse.
func Open() (*Report, error) {
	return OpenFlagSet(flag.CommandLine, os.Args[1:])
}

// OpenFlagSet opens a report against a flag set the caller owns. It is the
// entry point for an evaluator that dispatches subcommands, where each
// subcommand parses its own argv into its own [flag.FlagSet].
//
// OpenFlagSet registers -vs-output on fs unless it is registered already,
// parses args into fs unless fs is parsed already, and then opens the parsed
// path, as [OpenPath] does. A flag set the caller already parsed is used as it
// stands and args is ignored.
func OpenFlagSet(fs *flag.FlagSet, args []string) (*Report, error) {
	f := fs.Lookup(OutputFlag)
	if f == nil {
		if fs.Parsed() {
			return nil, fmt.Errorf(
				"flags were parsed without -%s registered: call vseval.RegisterFlags on the flag set before parsing it, or use vseval.OpenPath",
				OutputFlag,
			)
		}
		RegisterFlags(fs)
		f = fs.Lookup(OutputFlag)
	}
	if !fs.Parsed() {
		if err := fs.Parse(args); err != nil {
			return nil, err
		}
	}
	return OpenPath(f.Value.String())
}

// RegisterFlags registers -vs-output on fs and returns the pointer that
// receives the parsed path. Use it when the evaluator owns its own flag set or
// parses flag.CommandLine itself.
func RegisterFlags(fs *flag.FlagSet) *string {
	return fs.String(OutputFlag, "", outputFlagUsage)
}

// OpenPath creates or truncates path and returns the report that writes to it.
// Use it when the caller already parsed the output path.
//
// An empty path means no report was requested. Reporting is optional so that
// an evaluator run by hand behaves like one run by the framework: OpenPath
// returns a report that discards every record instead of an error, no file is
// created, and [Report.Reporting] reports false. An evaluator that must not be
// run unreported should check [Report.Reporting] and say so itself.
func OpenPath(path string) (*Report, error) {
	if path == "" {
		return newReport(io.Discard, nil, false), nil
	}
	file, err := os.Create(path)
	if err != nil {
		return nil, fmt.Errorf("open evaluator output %q: %w", path, err)
	}
	return newReport(file, file, true), nil
}

// OpenWriter returns a report that writes to w. The caller owns w:
// [Report.Close] does not close it. It is the seam tests and embedders use to
// capture a stream without touching the filesystem.
func OpenWriter(w io.Writer) *Report {
	return newReport(w, nil, true)
}

func newReport(w io.Writer, closer io.Closer, reporting bool) *Report {
	return &Report{w: w, closer: closer, reporting: reporting, exit: os.Exit}
}

// Declare writes the hello record for s and returns the run that measures it.
// It is the second reporting phase; call it as soon as the metric names, units,
// and directions are known.
//
// The hello record is written and flushed before Declare returns, not at
// [Run.Emit]. If the evaluator crashes mid-run the stream still carries the
// schema, so a reader reports a missing outcome ("it started and died") rather
// than a missing hello ("this producer does not speak the protocol").
//
// Declare fails, writing nothing, if the schema is empty or carries a
// declaration error, and it can be called only once per report. A failed
// Declare leaves the stream without an outcome, so the caller can still report
// the failure with [Report.EmitError] or [Report.Fail].
func (r *Report) Declare(s *Schema) (*Run, error) {
	if r.outcome {
		return nil, errOutcomeWritten
	}
	if r.declared {
		return nil, errors.New("a schema was already declared: a stream carries exactly one hello record")
	}
	if err := s.check(); err != nil {
		return nil, err
	}
	metrics := make(map[string]*evaluatorv1.MetricSpec, len(s.names))
	for i, name := range s.names {
		metrics[name] = s.specs[i].record()
	}
	if err := r.write(&evaluatorv1.ResultRecord{Kind: ptr(kindHello), Protocol: ptr(uint32(Protocol)), Metrics: metrics}); err != nil {
		return nil, err
	}
	r.declared = true
	s.declared = true
	return &Run{
		report: r,
		schema: s,
		values: make([]float64, len(s.names)),
		set:    make([]bool, len(s.names)),
	}, nil
}

// Reporting reports whether this report writes a stream anyone will read. It is
// false for one opened without an output path, which discards every record so
// that measurement code needs no branch of its own.
//
// Branch on it only for behavior that the report itself constrains. Protocol 2
// carries a single result row, for example, so an evaluator that can sweep
// several operating points has to narrow the sweep to one when it reports, and
// need not when it does not.
func (r *Report) Reporting() bool { return r.reporting }

// OutcomeWritten reports whether the stream already carries its outcome, that
// is a result or an error record. A second outcome is rejected, so a deferred
// failure reporter asks this instead of tracking it itself:
//
//	defer func() {
//		if err != nil && !report.OutcomeWritten() {
//			report.EmitError(err)
//		}
//	}()
func (r *Report) OutcomeWritten() bool { return r.outcome }

// EmitError writes an error record and flushes it; like [Run.Emit] it leaves
// the close to the caller. Use it when the evaluator cannot produce a row. It
// is valid before a schema is declared: an error record on its own is a
// complete stream.
//
// A nil cause, or one with a blank message, is reported as
// [FailureFallbackMessage] so the stream stays valid.
func (r *Report) EmitError(cause error) error {
	if r.outcome {
		return errOutcomeWritten
	}
	message := ""
	if cause != nil {
		message = strings.TrimSpace(cause.Error())
	}
	if message == "" {
		message = FailureFallbackMessage
	}
	if err := r.write(&evaluatorv1.ResultRecord{Kind: ptr(kindError), Message: &message}); err != nil {
		return err
	}
	r.outcome = true
	return nil
}

// Fail writes an error record and terminates the process with a non-zero
// status. It is the terminal form of [Report.EmitError] for evaluators whose
// main function has nothing left to do; a failure to write the record is
// reported on stderr, since no one is left to receive it.
//
// Fail calls os.Exit, so no deferred function runs, including a deferred
// [Report.Close]. Use [Report.EmitError] instead wherever the caller has
// deferred cleanup or an error to return, which is every path but the
// outermost one.
func (r *Report) Fail(cause error) {
	if err := r.EmitError(cause); err != nil {
		fmt.Fprintf(os.Stderr, "vseval: %v\n", err)
	}
	r.exit(1)
}

// Close releases the output file if this report opened one. Defer it right
// after opening: it is the only thing that closes the output, and it is
// idempotent, so a second call after an early one is harmless. A report opened
// with [OpenWriter], or one that is not reporting, owns no file and Close is a
// no-op.
func (r *Report) Close() error {
	if r.closer == nil {
		return nil
	}
	closer := r.closer
	r.closer = nil
	return closer.Close()
}

var errOutcomeWritten = errors.New("the outcome record was already written")

// flusher is implemented by buffered writers such as *bufio.Writer. Records
// must reach the file as they are produced, not when the process exits.
type flusher interface {
	Flush() error
}

func (r *Report) write(record *evaluatorv1.ResultRecord) error {
	line, err := encode(record)
	if err != nil {
		return err
	}
	if _, err := r.w.Write(append(line, '\n')); err != nil {
		return fmt.Errorf("write evaluator record: %w", err)
	}
	if f, ok := r.w.(flusher); ok {
		if err := f.Flush(); err != nil {
			return fmt.Errorf("flush evaluator record: %w", err)
		}
	}
	return nil
}
