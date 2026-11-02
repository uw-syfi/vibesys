package reading

import (
	"fmt"
	"math"
	"strconv"
)

const (
	// MaxFileBytes is the most a caller should read of a benchmark's output file; the readers
	// reject nothing for size, so a caller that reads more is deciding to hold it in memory.
	MaxFileBytes = 16 << 20
	// MaxLineBytes is the largest record line a protocol stream may hold.
	MaxLineBytes = 4 << 20
	// ProtocolVersion is the evaluator result protocol this reader implements.
	ProtocolVersion = 2
)

// Named is one measured value and the metric it measures.
type Named struct {
	Name  string
	Value float64
}

// Reading is what one benchmark run measured: the value of the scored metric and the other
// metrics the run reported beside it, ordered by name. Every value is finite.
type Reading struct {
	Value  float64
	Others []Named
}

// Code says why a reader rejected an output. The protocol codes are the protocol's own, shared by
// every conforming reader; the last three are for the JSON file contract.
type Code string

// The reason codes.
const (
	MissingHello        Code = "MISSING_HELLO"
	HelloNotFirst       Code = "HELLO_NOT_FIRST"
	DuplicateHello      Code = "DUPLICATE_HELLO"
	UnsupportedProtocol Code = "UNSUPPORTED_PROTOCOL"
	EmptyMetrics        Code = "EMPTY_METRICS"
	InvalidMetricName   Code = "INVALID_METRIC_NAME"
	NoOutcome           Code = "NO_OUTCOME"
	UnknownMetric       Code = "UNKNOWN_METRIC"
	MissingMetric       Code = "MISSING_METRIC"
	NonNumericValue     Code = "NON_NUMERIC_VALUE"
	NonFiniteValue      Code = "NON_FINITE_VALUE"
	UnsupportedLabel    Code = "UNSUPPORTED_LABEL"
	UnknownKind         Code = "UNKNOWN_KIND"
	UnknownKey          Code = "UNKNOWN_KEY"
	MalformedLine       Code = "MALFORMED_LINE"
	InvalidRecord       Code = "INVALID_RECORD"

	// MalformedFile is a result file that is not one JSON object or list of objects.
	MalformedFile Code = "MALFORMED_FILE"
	// AmbiguousMetric is a list in which several entries carry the metric, so no one value is the
	// run's.
	AmbiguousMetric Code = "AMBIGUOUS_METRIC"
)

// Rejection is an output the reader does not accept.
type Rejection struct {
	Code    Code
	Message string
}

// Error implements error.
func (r *Rejection) Error() string { return string(r.Code) + ": " + r.Message }

func reject(code Code, format string, args ...any) *Rejection {
	return &Rejection{Code: code, Message: fmt.Sprintf(format, args...)}
}

// Reported is a run that told the reader it produced no measurement: the benchmark itself wrote an
// error record. It is a valid stream, and it is what a benchmark says when it knows it failed.
type Reported struct {
	Message string
	// Requirement names a capability the measured revision lacks when the record says that
	// measuring it again cannot help; it is empty for any other error.
	Requirement string
}

// Error implements error.
func (r *Reported) Error() string {
	if r.Requirement != "" {
		return fmt.Sprintf("the benchmark reported that requirement %s is not met: %s", r.Requirement, r.Message)
	}
	return "the benchmark reported an error: " + r.Message
}

// number reads a JSON number token as a finite float. A token that overflows a float64 is a
// non-finite value, which is how the protocol treats 1e999.
func number(token string) (float64, bool) {
	f, err := strconv.ParseFloat(token, 64)
	if err != nil || math.IsInf(f, 0) || math.IsNaN(f) {
		return 0, false
	}
	return f, true
}

// isNumberToken reports whether a raw JSON value is a number, and not a string, boolean, null,
// object or array.
func isNumberToken(raw []byte) bool {
	return len(raw) > 0 && (raw[0] == '-' || (raw[0] >= '0' && raw[0] <= '9'))
}
