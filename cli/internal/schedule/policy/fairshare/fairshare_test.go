package fairshare_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fairshare"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/scheduletest"
)

func TestFairSharePassesThePolicyContract(t *testing.T) {
	scheduletest.Run(t, scheduletest.Contract{New: fairshare.New, Fair: true})
}
