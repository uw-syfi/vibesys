package core_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A measurement pooled from several jobs keeps the unit of the metric and of each observation, and
// two measurements of one commit counted in different units stay apart: they are not one kind.
func TestAPooledMeasurementKeepsItsUnits(t *testing.T) {
	job := func(id, unit string) core.Result {
		r := measurement(id, candidateCommit, []float64{1, 2}, 5, checkedBy("ck"))
		o := r.Outcome.(core.MeasureOutcome)
		o.Metric.Unit = unit
		o.Observations = []core.Observation{{Name: "ttft", Values: []float64{3}, Unit: "ms"}}
		r.Outcome = o
		return r
	}
	var pooled []core.Measurement
	for _, m := range core.PoolMeasurements([]core.Result{job("a", "tok/s"), job("b", "tok/s")}) {
		pooled = append(pooled, m)
	}
	if len(pooled) != 1 || pooled[0].Outcome.Metric.Unit != "tok/s" || len(pooled[0].Outcome.Observations) != 1 || pooled[0].Outcome.Observations[0].Unit != "ms" {
		t.Fatalf("pooled %+v", pooled)
	}
	if apart := core.PoolMeasurements([]core.Result{job("a", "tok/s"), job("b", "req/s")}); len(apart) != 2 {
		t.Fatalf("measurements in different units pooled into %d", len(apart))
	}
}
