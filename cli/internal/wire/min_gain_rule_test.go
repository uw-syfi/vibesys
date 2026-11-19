package wire_test

import (
	"math"
	"math/rand/v2"
	"testing"

	"buf.build/go/protovalidate"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Every min_gain objectives.toml accepts (at least 0, below 1) is saved as a message its rules
// accept, and a value the task file would refuse, or an objective with no name, is refused here
// too, so a damaged record cannot carry a bound the comparison would never have used.
func TestSavedMinGainsFollowTheTaskFilesRule(t *testing.T) {
	r := rand.New(rand.NewPCG(4324, 1))
	for range 2000 {
		g := r.Float64() // [0, 1)
		msg := wire.Objectives(core.Objectives{MinGains: []core.ObjectiveMinGain{{Name: "throughput", MinGain: g}}})
		if err := protovalidate.Validate(msg); err != nil {
			t.Fatalf("min_gain %v: %v", g, err)
		}
	}
	for _, bad := range []core.ObjectiveMinGain{
		{Name: "throughput", MinGain: -1e-9},
		{Name: "throughput", MinGain: 1},
		{Name: "throughput", MinGain: 1 + r.Float64()},
		{Name: "throughput", MinGain: math.NaN()},
		{Name: "throughput", MinGain: math.Inf(1)},
		{Name: "", MinGain: 0.02},
	} {
		msg := wire.Objectives(core.Objectives{MinGains: []core.ObjectiveMinGain{bad}})
		if err := protovalidate.Validate(msg); err == nil {
			t.Fatalf("%+v was accepted", bad)
		}
	}
}
