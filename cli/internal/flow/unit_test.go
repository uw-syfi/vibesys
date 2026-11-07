package flow_test

import (
	"fmt"
	"math/rand/v2"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
)

// Whatever units a task declares, the result a measure job saves records each: the scored metric's
// unit, and for every observation the unit of the objective of its name. An observation that no
// objective names has no unit, and what the benchmark reported is never changed.
func TestAResultRecordsTheUnitsTheTaskDeclares(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 8))
	units := []string{"", "tok/s", "ms", "GB/s", "µs"}
	for range 300 {
		p := shape{kind: core.JobMeasure, protocol2: true, samples: 1}.plan()
		p.Metric.Unit = units[r.IntN(len(units))]
		declared := map[string]string{}
		var found []core.Observation
		for i := range 1 + r.IntN(4) {
			name := fmt.Sprintf("obs%d", i)
			if r.IntN(4) != 0 { // most observations are of declared objectives
				unit := units[r.IntN(len(units))]
				p.Others = append(p.Others, core.Metric{Name: name, Direction: core.Minimize, Unit: unit})
				declared[name] = unit
			}
			found = append(found, core.Observation{Name: name, Values: []float64{float64(i + 1)}})
		}
		res := p.Record("j", commit, flow.Findings{Samples: []float64{1}, Observations: found}, epoch)
		out := res.Outcome.(core.MeasureOutcome)
		if out.Metric.Unit != p.Metric.Unit {
			t.Fatalf("scored unit %q, want %q", out.Metric.Unit, p.Metric.Unit)
		}
		for i, o := range out.Observations {
			if o.Unit != declared[o.Name] || o.Name != found[i].Name || found[i].Unit != "" {
				t.Fatalf("observation %+v from %+v, declared %v", o, found[i], declared)
			}
		}
	}
}
