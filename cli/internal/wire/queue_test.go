package wire_test

import (
	"fmt"
	"math/rand/v2"
	"testing"

	"buf.build/go/protovalidate"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Every wait the daemon can say passes the contract's own rules, so a client that validates what it
// reads takes it.
func TestEveryWaitIsAValidMessage(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	kinds := []core.JobKind{"", core.JobCheck, core.JobCommand, core.JobMeasure}
	for i := range 2000 {
		kind := core.WaitKinds()[r.IntN(len(core.WaitKinds()))]
		w := core.JobWait{Kind: kind}
		switch kind {
		case core.WaitHeld, core.WaitTrouble:
			w.Detail = fmt.Sprintf("why %d", i)
		default:
			w.Resource = "placement:default"
			for n := range 1 + r.IntN(3) {
				w.Holders = append(w.Holders, core.WaitHolder{Job: core.JobID(fmt.Sprintf("j%d", n+1)), Task: "t", Kind: kinds[r.IntN(len(kinds))], Run: core.RunID([]string{"", "run-a"}[r.IntN(2)])})
			}
		}
		jobs := []core.Job{{ID: "j9"}}
		out := wire.JobWaits(jobs, map[core.JobID]core.JobWait{"j9": w})
		if len(out) != 1 {
			t.Fatalf("%+v: %d waits", w, len(out))
		}
		if err := protovalidate.Validate(out[0]); err != nil {
			t.Fatalf("%+v: %v", w, err)
		}
	}
}
