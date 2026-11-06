package evaluator

import (
	"fmt"
	"slices"
)

// Aggregation says how the trials of one metric combine into the sample's value.
type Aggregation string

// The aggregations. Median is Python's statistics.median: the middle trial, or the mean of the
// two middle ones.
const (
	Median Aggregation = "median"
	Mean   Aggregation = "mean"
	MinOf  Aggregation = "min"
	MaxOf  Aggregation = "max"
	Sum    Aggregation = "sum"
)

// Combine is one value from a metric's trials, which must be non-empty and finite.
func Combine(how Aggregation, trials []float64) (float64, error) {
	if len(trials) == 0 {
		return 0, fmt.Errorf("combine %s: no trials", how)
	}
	for _, v := range trials {
		if !Finite(v) {
			return 0, fmt.Errorf("combine %s: a trial is %s; every trial must be finite", how, pyFloat(v))
		}
	}
	switch how {
	case Median:
		sorted := slices.Sorted(slices.Values(trials))
		n := len(sorted)
		if n%2 == 1 {
			return sorted[n/2], nil
		}
		return (sorted[n/2-1] + sorted[n/2]) / 2, nil
	case Mean:
		return sum(trials) / float64(len(trials)), nil
	case MinOf:
		return slices.Min(trials), nil
	case MaxOf:
		return slices.Max(trials), nil
	case Sum:
		return sum(trials), nil
	}
	return 0, fmt.Errorf("unknown aggregation %q: use median, mean, min, max or sum", string(how))
}

func sum(values []float64) float64 {
	total := 0.0
	for _, v := range values {
		total += v
	}
	return total
}
