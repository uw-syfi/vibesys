package engine

import "testing"

func TestPercentileUsesLinearInterpolation(t *testing.T) {
	values := []float64{1, 2, 3, 4}
	if got := percentile(values, 50); got != 2.5 {
		t.Fatalf("p50 = %v, want 2.5", got)
	}
}

func TestAggregateTreatsTrialsAsUnits(t *testing.T) {
	result := aggregate([]float64{10, 11, 50})
	if result.Median == nil || *result.Median != 11 {
		t.Fatalf("unexpected median: %+v", result)
	}
	if result.MAD == nil || *result.MAD != 1 {
		t.Fatalf("unexpected MAD: %+v", result)
	}
}
