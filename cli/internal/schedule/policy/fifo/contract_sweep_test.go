//go:build !race

package fifo_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/scheduletest"
)

func TestFIFOPassesThePolicyContract(t *testing.T) {
	scheduletest.Run(t, scheduletest.Contract{New: fifo.New})
}
