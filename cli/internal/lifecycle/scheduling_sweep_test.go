//go:build !race

package lifecycle_test

import "testing"

func TestWithSeveralSlotsEveryEventSequenceKeepsTheClaimsAndMakesProgress(t *testing.T) {
	for seed := range uint64(400) {
		w := newScheduledWorld(t, seed, 1+int(seed%3))
		w.churn(120, false)
		w.drain()
	}
}
