package engine

import (
	"math/rand/v2"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
)

// poissonStream is the PCG stream constant of the Poisson arrivals; with the seed it fixes them.
const poissonStream = 0x9e3779b97f4a7c15

// NewArrivals returns successive arrival offsets from t0: the first is 0, then uniform arrivals
// are 1/rate apart and Poisson ones exponentially distributed with mean 1/rate (PCG from seed).
func NewArrivals(kind pb.Arrivals, rate float64, seed uint64) func() time.Duration {
	var elapsed float64 // seconds since t0
	var count float64
	switch kind {
	case pb.Arrivals_ARRIVALS_POISSON:
		random := rand.New(rand.NewPCG(seed, poissonStream))
		return func() time.Duration {
			offset := elapsed
			elapsed += random.ExpFloat64() / rate
			return time.Duration(offset * float64(time.Second))
		}
	default:
		return func() time.Duration {
			// Index times period, not a running sum, so rounding does not drift.
			offset := count / rate
			count++
			return time.Duration(offset * float64(time.Second))
		}
	}
}
