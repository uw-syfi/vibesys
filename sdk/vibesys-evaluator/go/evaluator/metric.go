package evaluator

import (
	"fmt"
	"math"
	"strings"
	"unicode"
)

// Direction says which way a metric is better.
type Direction string

// The two directions of the result protocol.
const (
	Max Direction = "max"
	Min Direction = "min"
)

// Metric is a metric the benchmark reports (declared in the hello record).
type Metric struct {
	// Unit is for people ("ops/s", "ms"); may be empty.
	Unit string
	// Direction says which way is better.
	Direction Direction
}

// Validate rejects a direction the result protocol does not have.
func (m Metric) Validate() error {
	if m.Direction != Max && m.Direction != Min {
		return fmt.Errorf("Metric direction is 'max' or 'min', got %s", pyRepr(string(m.Direction)))
	}
	return nil
}

// NamedMetric is a declared metric with its name, in declaration order.
type NamedMetric struct {
	Name string
	Metric
}

// Validity is when a phase is a measurement; a phase that breaks a rule fails the sample.
type Validity struct {
	// MaxErrorRate is the most errors (the load generator's: transport, timeout, status,
	// capture) over the window's operations a phase may have, in [0, 1].
	MaxErrorRate float64
	// MinSustainedRatio is, open loop, the least offered rate over target rate, in (0, 1].
	MinSustainedRatio float64
	// MinOpsPerOperation is the least number of times every operation of the mix completed in
	// the window.
	MinOpsPerOperation int
}

// DefaultValidity is the Validity both yardsticks use: no errors, 95% sustained, every
// operation at least once.
func DefaultValidity() Validity {
	return Validity{MaxErrorRate: 0, MinSustainedRatio: 0.95, MinOpsPerOperation: 1}
}

// Validate rejects rules no phase could meet or that mean nothing.
func (v Validity) Validate() error {
	if !(v.MaxErrorRate >= 0 && v.MaxErrorRate <= 1) {
		return fmt.Errorf("Validity max_error_rate is in [0, 1], got %s", pyFloat(v.MaxErrorRate))
	}
	if !(v.MinSustainedRatio > 0 && v.MinSustainedRatio <= 1) {
		return fmt.Errorf("Validity min_sustained_ratio is in (0, 1], got %s", pyFloat(v.MinSustainedRatio))
	}
	if v.MinOpsPerOperation < 0 {
		return fmt.Errorf("Validity min_ops_per_operation is an int >= 0, got %d", v.MinOpsPerOperation)
	}
	return nil
}

// ValidMetricName reports whether name is a result-protocol metric name: non-empty, no whitespace.
func ValidMetricName(name string) bool {
	return name != "" && !strings.ContainsFunc(name, unicode.IsSpace)
}

// Finite reports whether v is a finite number.
func Finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
